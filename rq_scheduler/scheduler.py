import signal
import time
from datetime import datetime

try:
    from logbook import Logger
    Logger = Logger   # Does nothing except it shuts up pyflakes annoying error
except ImportError:
    from logging import Logger

from rq.connections import get_current_connection
from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.queue import Queue

from redis import WatchError


class Scheduler(object):
    scheduler_key = 'rq:scheduler'
    scheduled_jobs_key = 'rq:scheduler:scheduled_jobs'

    def __init__(self, queue_name='default', interval=60, connection=None):
        if connection is None:
            connection = get_current_connection()
        self.connection = connection
        self.queue_name = queue_name
        self._interval = interval
        self.log = Logger('scheduler')

    def register_birth(self):
        if self.connection.exists(self.scheduler_key) and \
                not self.connection.hexists(self.scheduler_key, 'death'):
            raise ValueError("There's already an active RQ scheduler")
        key = self.scheduler_key
        now = time.time()
        with self.connection.pipeline() as p:
            p.delete(key)
            p.hset(key, 'birth', now)
            p.execute()

    def register_death(self):
        """Registers its own death."""
        with self.connection.pipeline() as p:
            p.hset(self.scheduler_key, 'death', time.time())
            p.expire(self.scheduler_key, 60)
            p.execute()

    def _install_signal_handlers(self):
        """
        Installs signal handlers for handling SIGINT and SIGTERM
        gracefully.
        """

        def stop(signum, frame):
            """
            Register scheduler's death and exit.
            """
            self.log.debug('Shutting down RQ scheduler...')
            self.register_death()
            raise SystemExit()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

    def _create_job(self, func, args=None, kwargs=None, commit=True,
                    result_ttl=None):
        """
        Creates an RQ job and saves it to Redis.
        """
        if func.__module__ == '__main__':
            raise ValueError(
                    'Functions from the __main__ module cannot be processed '
                    'by workers.')
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        job = Job.create(func, args=args, connection=self.connection,
                         kwargs=kwargs, result_ttl=result_ttl)
        job.origin = self.queue_name
        if commit:
            job.save()
        return job

    def enqueue_at(self, scheduled_time, func, *args, **kwargs):
        """
        Pushes a job to the scheduler queue. The scheduled queue is a Redis sorted
        set ordered by timestamp - which in this case is job's scheduled execution time.

        Usage:

        from datetime import datetime
        from redis import Redis
        from rq.scheduler import Scheduler

        from foo import func

        redis = Redis()
        scheduler = Scheduler(queue_name='default', connection=redis)
        scheduler.enqueue_at(datetime(2020, 1, 1), func, 'argument', keyword='argument')
        """
        job = self._create_job(func, args=args, kwargs=kwargs)
        self.connection.zadd(self.scheduled_jobs_key, job.id,
                             int(scheduled_time.strftime('%s')))
        return job

    # This is here for backwards compatibility purposes
    schedule = enqueue_at

    def enqueue_in(self, time_delta, func, *args, **kwargs):
        """
        Similar to ``enqueue_at``, but accepts a timedelta instead of datetime object.
        The job's scheduled execution time will be calculated by adding the timedelta
        to datetime.now().
        """
        job = self._create_job(func, args=args, kwargs=kwargs)
        self.connection.zadd(self.scheduled_jobs_key, job.id,
                             int((datetime.now() + time_delta).strftime('%s')))
        return job

    def enqueue_periodic(self, scheduled_time, interval, repeat, func,
                         *args, **kwargs):
        """
        Schedule a job to be periodically executed, at a certain interval.
        """
        return self.enqueue(scheduled_time, func, args=args, kwargs=kwargs,
                            interval=interval, repeat=repeat)

    def enqueue(self, scheduled_time, func, args=None, kwargs=None,
                interval=None, repeat=None, result_ttl=None):
        """
        Schedule a job to be periodically executed, at a certain interval.
        """
        # Set result_ttl to -1 for periodic jobs, if result_ttl not specified
        if interval is not None and result_ttl is None:
            result_ttl = -1
        job = self._create_job(func, args=args, kwargs=kwargs, commit=False,
                               result_ttl=result_ttl)
        if interval is not None:
            job.interval = int(interval)
        if repeat is not None:
            job.repeat = int(repeat)
        if repeat and interval is None:
            raise ValueError("Can't repeat a job without interval argument")
        job.save()
        self.connection.zadd(self.scheduled_jobs_key, job.id,
                             int(scheduled_time.strftime('%s')))
        return job

    def cancel(self, job):
        """
        Pulls a job from the scheduler queue. This function accepts either a
        job_id or a job instance.
        """
        if isinstance(job, basestring):
            self.connection.zrem(self.scheduled_jobs_key, job)
        else:
            self.connection.zrem(self.scheduled_jobs_key, job.id)

    def change_execution_time(self, job, date_time):
        """
        Change a job's execution time. Wrap this in a transaction to prevent race condition.
        """
        with self.connection.pipeline() as pipe:
            while 1:
                try:
                    pipe.watch(self.scheduled_jobs_key)
                    if pipe.zscore(self.scheduled_jobs_key, job.id) is None:
                        raise ValueError('Job not in scheduled jobs queue')
                    pipe.zadd(self.scheduled_jobs_key, job.id, int(date_time.strftime('%s')))
                    break
                except WatchError:
                    # If job is still in the queue, retry otherwise job is already executed
                    # so we raise an error
                    if pipe.zscore(self.scheduled_jobs_key, job.id) is None:
                        raise ValueError('Job not in scheduled jobs queue')
                    continue

    def get_jobs_to_queue(self):
        """
        Returns a list of job instances that should be queued
        (score lower than current timestamp).
        """
        job_ids = self.connection.zrangebyscore(self.scheduled_jobs_key, 0, int(time.strftime('%s')))
        jobs = []
        for job_id in job_ids:
            try:
                jobs.append(Job.fetch(job_id, connection=self.connection))
            except NoSuchJobError:
                # Delete jobs that aren't there from scheduler
                self.cancel(job_id)
        return jobs


    def get_queue_for_job(self, job):
        """
        Returns a queue to put job into.
        """
        key = '{0}{1}'.format(Queue.redis_queue_namespace_prefix, job.origin)
        return Queue.from_queue_key(key, connection=self.connection)

    def enqueue_job(self, job):
        """
        Move a scheduled job to a queue. In addition, it also does puts the job
        back into the scheduler if needed.
        """
        self.log.debug('Pushing {0} to {1}'.format(job.id, job.origin))

        interval = getattr(job, 'interval', None)
        repeat = getattr(job, 'repeat', None)

        # If job is a repeated job, decrement counter
        if repeat:
            job.repeat = int(repeat) - 1
        job.enqueued_at = datetime.now()
        job.save()

        queue = self.get_queue_for_job(job)
        queue.push_job_id(job.id)
        self.connection.zrem(self.scheduled_jobs_key, job.id)

        if interval:
            # If this is a repeat job and counter has reached 0, don't repeat
            if repeat is not None:
                if job.repeat == 0:
                    return
            self.connection.zadd(self.scheduled_jobs_key, job.id,
                int(datetime.now().strftime('%s')) + int(interval))

    def enqueue_jobs(self):
        """
        Move scheduled jobs into queues.
        """
        jobs = self.get_jobs_to_queue()
        for job in jobs:
            self.enqueue_job(job)
        return jobs

    def run(self):
        """
        Periodically check whether there's any job that should be put in the queue (score
        lower than current time).
        """
        self.log.debug('Running RQ scheduler...')
        self.register_birth()
        self._install_signal_handlers()
        try:
            while True:
                self.enqueue_jobs()
                time.sleep(self._interval)
        finally:
            self.register_death()

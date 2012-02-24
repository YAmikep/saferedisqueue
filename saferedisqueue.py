# -*- coding: utf-8 -*-
"""
saferedisqueue
~~~~~~~~~~~~~~

A small wrapper around Redis that provides access to a FIFO queue and allows
upstream code to mark pop'ed items as processed successfully (ACK'ing) or
unsucessfully (FAIL'ing). Failed items are automatically requeued. Addionally
a backup is kept for items that were neither ack'ed nor fail'ed, i.e. in case
the consumer crashes. The backup items will be requeued as soon as one of the
consumer(s) comes up again.

All actions are atomic and it is safe to have multiple producers and consumers
accessing the same queue concurrently.

:copyright: (c) Ferret Go GmbH, Sean Buttinger, Fabian Neumann
:license: All Rights Reserved. For now.
"""

__version__ = '0.2.0-alpha'


import uuid

import redis


class SafeRedisQueue(object):

    AUTOCLEAN_TIMEOUT = 60

    def __init__(self, *args, **kw):
        prefix = kw.pop('prefix', 'srq')
        self.QUEUE_KEY = '%s:queue' % prefix
        self.ITEMS_KEY = '%s:items' % prefix
        self.ACKBUF_KEY = '%s:ackbuf' % prefix
        self.BACKUP = '%s:backup' % prefix
        self.BACKUP_LOCK = '%s:backuplock' % prefix
        self._redis = redis.StrictRedis(*args, **kw)

    def _autoclean(self):
        if self._redis.exists(self.BACKUP_LOCK):
            return
        if self._redis.exists(self.BACKUP):
            # Get the lock
            if self._redis.setnx(self.BACKUP_LOCK, 1):
                # There's a potential deadlock here:
                # If we crash during the while loop, the BACKUP_LOCK will never
                # be set to expire, blocking the creation of a new backup queue.
                # Could be solved by setting the expire immediately, although
                # then the guard could expire before we are done with the
                # requeuing. Although that is very unlikely.
                while self._redis.rpoplpush(self.BACKUP, self.QUEUE_KEY):
                    pass
                self._redis.pipeline()\
                    .rename(self.ACKBUF_KEY, self.BACKUP)\
                    .expire(self.BACKUP_LOCK, self.AUTOCLEAN_TIMEOUT)\
                    .execute()
        else:
            with self._redis.pipeline() as pipe:
                try:
                    pipe.watch(self.BACKUP_LOCK)
                    if not pipe.exists(self.BACKUP_LOCK):
                        pipe.multi()
                        pipe.renamenx(self.ACKBUF_KEY, self.BACKUP)\
                            .setex(self.BACKUP_LOCK, self.AUTOCLEAN_TIMEOUT, 1)\
                            .execute()
                    else:
                        pipe.unwatch()
                except redis.WatchError:
                    pass

    def push(self, item):
        """Adds an item to the queue.

        Generates a uid for later referral.
        Enqueues the uid and stores the item.
        """
        uid = uuid.uuid4()
        self._redis.pipeline()\
                .hset(self.ITEMS_KEY, uid, item)\
                .lpush(self.QUEUE_KEY, uid)\
                .execute()

    def pop(self, timeout=0):
        """Get next item from queue. Blocks if queue is empty.

        Pops uid from queue and writes it to ackbuffer.
        Returns the corresponding item.
        """
        self._autoclean()
        uid = self._redis.brpoplpush(self.QUEUE_KEY, self.ACKBUF_KEY, timeout)
        item = self._redis.hget(self.ITEMS_KEY, uid)
        return uid, item

    def ack(self, uid):
        """Acknowledge item as successfully consumed.

        Removes uid from ackbuffer and deletes the corresponding item.
        """
        self._redis.pipeline()\
                   .lrem(self.ACKBUF_KEY, 0, uid)\
                   .lrem(self.BACKUP, 0, uid)\
                   .hdel(self.ITEMS_KEY, uid)\
                   .execute()

    def fail(self, uid):
        """Report item as not successfully consumed.

        Removes uid from ackbuffer and re-enqueues it.
        """
        self._redis.pipeline()\
                   .lrem(self.ACKBUF_KEY, 0, uid)\
                   .lrem(self.BACKUP, 0, uid)\
                   .lpush(self.QUEUE_KEY, uid)\
                   .execute()


if __name__ == "__main__":
    import sys

    def _usage():
        sys.stdout.writelines([
            'Commandline usage: python saferedisqueue.py <consumer | producer | demo>\n'
            'Example:\n'
            '    $ echo "Hello World" | python saferedisqueue.py producer\n'
            '    $ python saferedisqueue.py consumer\n'
            '    cbdabbc8-1c0f-4eb0-8733-fdb62a9c0fa6 Hello World\n'
        ])
        sys.exit(1)

    if len(sys.argv) != 2:
        _usage()

    queue = SafeRedisQueue()

    if sys.argv[1] == 'producer':
        for line in sys.stdin.readlines():
            queue.push(line.strip())
    elif sys.argv[1] == 'consumer':
        while True:
            uid, item = queue.pop()
            queue.ack(uid)
            print uid, item
    elif sys.argv[1] == 'demo':
        map(queue.push, ['Hello', 'World'])
        while True:
            uid, item = queue.pop(timeout=1)
            if uid is None:
                sys.exit()
            queue.ack(uid)
            print uid, item
    else:
        _usage()

''' Context manager to asynchronously run functions or methods on the second core (using `_threads`)

    Copyright (c) 2025 Harm Lammers

    This is a reworked/integrated/optimized version of
    https://github.com/peterhinch/micropython-async/blob/master/v3/threadsafe/context.py and
    https://github.com/peterhinch/micropython-async/blob/master/v3/threadsafe/threadsafe_queue.py,
    copyright (c) Peter Hinch, published under MIT licence

    MIT licence:

    Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to
    the following conditions:

    The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
    MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
    CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
    SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
'''

import micropython
import asyncio
import _thread
from collections import deque

from singleton import singleton

_g_run = True # flag to indicate that the loop on the second core should continue running

class _Job:
    '''Object describing a job running on the second core

        Args:
            func (Callable): Function to be queued
            args (tuple): Positional arguments to pass to `func`
            kwargs (dict): Keyword arguments to pass to `func`
    '''
    def __init__(self, func, args: tuple, kwargs: dict):
        self.kwargs = kwargs
        self.args = args
        self.func = func
        self.return_val = None
        self.done = asyncio.ThreadSafeFlag()

@singleton
class Context:
    ''' Singleton context manager to asynchronously run functions or methods on the second core (using `_threads`)

    Args:
        queue_size (int, optional): Size of the deque buffer to store tasks to be queued to be run on the second core; defaults to 10
    '''
    def __init__(self, queue_size:int = 10) -> None:
        self._deque = deque((), queue_size)
        self.size = queue_size
        self._put_flag = asyncio.ThreadSafeFlag()
        self._get_flag = asyncio.ThreadSafeFlag()
        _thread.start_new_thread(_second_core_loop, (self,))

    async def assign(self, func, *args, **kwargs):
        ''' `asyncio` awaitable to queue function to be run on the second core; releases once the function completed running

        Args:
            func (Callable): Function to be queued
            *args: Positional arguments to pass to `func`
            **kwargs: Keyword arguments to pass to `func`

        Returns:
            Any: Return value(s) from `func`
        '''
        job = _Job(func, args, kwargs)
        await self._put(job) # pause if deque is full
        await job.done.wait()
        return job.return_val

    def deinit(self) -> None:
        ''' Stop second core context '''
        global _g_run
        _g_run = False

    @micropython.viper
    def _get(self):
        ''' Get next job from queue to run on second core

        Returns:
            _Job: Next job (function with arguments to pass)
        '''
        _deque = self._deque
        while not _deque:
            pass
        job = _deque.popleft()
        _get_flag = self._get_flag
        _get_flag.set()
        return job

    async def _put(self, job: _Job):
        ''' `asyncio` awaitable to put job to queue to run on second core; releases once job was successfully queued

        Args:
            job (_Job): Job (function with arguments to pass) to be queued
        '''
        _deque = self._deque
        _size = self.size
        _get_flag = self._get_flag
        while len(_deque) >= _size:
            await _get_flag.wait()
        _deque.append(job)
        _put_flag = self._put_flag
        _put_flag.set()

def _second_core_loop(queue: Context):
    ''' Eternal loop to run on second core executing jobs as they arrive

    Args:
        queue (Context): Context class instance (singleton) which contains the queue of jobs
    '''
    while _g_run:
        job = queue._get()
        job.return_val = job.func(*job.args, **job.kwargs)
        job.done.set()
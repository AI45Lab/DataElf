"""A cancellable, bounded read; the caller owns the shared intent deadline."""
from threading import Event, Thread
import socket
import urllib.error
import urllib.request

MAX_RESPONSE_BYTES = 1_048_576
MAX_ERROR_BYTES = 4096


def perform(request, *, timeout, deadline, clock, check_cancelled, opener=None):
    done, cancelled = Event(), Event()
    result = {}
    connection = {}

    def read():
        response = None
        try:
            try:
                response = (opener or urllib.request.urlopen)(request, timeout=timeout)
                status = getattr(response, 'status', 200)
            except urllib.error.HTTPError as error:
                response, status = error, error.code
            connection['response'] = response
            if cancelled.is_set():return
            headers = getattr(response, 'headers', None) or {}
            result.update(status=status, retry_after=headers.get('Retry-After'),
                          request_id=headers.get('x-request-id') or headers.get('request-id') or headers.get('x-correlation-id'))
            limit = MAX_ERROR_BYTES if status >= 400 else MAX_RESPONSE_BYTES + 1
            result['body'] = response.read(limit) or b''
        except (OSError, urllib.error.URLError) as error:
            result['transport_error'] = type(error).__name__
        except Exception as error:
            result['transport_error'] = type(error).__name__
        finally:
            try:
                if response is not None:response.close()
            finally:done.set()

    check_cancelled()
    Thread(target=read, name='serve-intent-http', daemon=True).start()
    try:
        while not done.wait(0.05):
            check_cancelled()
            if clock() >= deadline:raise TimeoutError('Intent total deadline exhausted')
        check_cancelled()
        if clock() >= deadline:raise TimeoutError('Intent total deadline exhausted')
        return result
    finally:
        cancelled.set()
        if not done.is_set():
            # Closing an HTTPResponse can block on its reader lock. Shutdown
            # its socket without acquiring that lock; the worker closes it.
            response = connection.get('response')
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if sock is not None:
                try:sock.shutdown(socket.SHUT_RDWR)
                except (OSError,ValueError):pass
        # A connect that has no response yet remains bounded by the original
        # remaining socket timeout. Its late result cannot advance this task.

import time


class RequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.perf_counter()
        response = self.get_response(request)
        ms = (time.perf_counter() - start) * 1000
        print(f'{request.method} {request.path} {response.status_code} {ms:.2f}ms', flush=True)
        return response

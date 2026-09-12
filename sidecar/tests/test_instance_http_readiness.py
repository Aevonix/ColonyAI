"""A ready instance may take over one second to assemble its health response."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

from pacomind.services.instance import InstanceService


def test_ready_instance_accepts_a_delayed_health_response(tmp_path, monkeypatch):
    requests = []

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get('Authorization')))
            # The actual restored service took 1.0235 seconds to return healthy.
            time.sleep(1.1)
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass  # A failing client timeout must not obscure the assertion.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('PACOMIND_SIDECAR_HOST', '127.0.0.1')
    monkeypatch.setenv('PACOMIND_SIDECAR_PORT', str(server.server_port))
    monkeypatch.setenv('PACOMIND_CLIENT_API_KEY', 'test-instance-client')
    service = InstanceService(tmp_path/'instance', tmp_path/'hermes', home=tmp_path/'user')
    try:
        assert service.healthy()
        assert requests == [('/v1/host/health', 'Bearer test-instance-client')]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

"""
One-shot token capture server.
Start this, then in Safari Console paste:
  fetch('http://localhost:8765/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(JSON.parse(localStorage.tokens))}).then(r=>r.text()).then(console.log)
"""
import http.server, json, pathlib, sys

TOKEN_FILE = pathlib.Path(__file__).parent / ".token_cache.json"

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        if self.path == "/token":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            TOKEN_FILE.write_text(json.dumps(data))
            print(f"\n✓ Token captured! access_token: {data.get('access_token','?')[:40]}...")
            print(f"  refresh_token: {data.get('refresh_token','?')[:40]}...")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"captured")
            sys.exit(0)
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/token":
            if TOKEN_FILE.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(TOKEN_FILE.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    port = 8765
    with http.server.HTTPServer(("localhost", port), Handler) as srv:
        print(f"Waiting for token on http://localhost:{port}/token ...")
        srv.serve_forever()

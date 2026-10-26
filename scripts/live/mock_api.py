"""Scripted stand-in for the Anthropic Messages API so a REAL Claude Code binary can be driven without a login/model.
Usage: mock_api.py PORT SCRIPT.json   (SCRIPT = list of Bash tool inputs: {"command": ..., "run_in_background": bool})
After the last scripted tool call it answers with text quoting the last tool_result. See scripts/live/run_live.sh."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PORT = int(sys.argv[1]); SCRIPT = json.load(open(sys.argv[2]))


def blocks(content):
    return content if isinstance(content, list) else [{"type": "text", "text": str(content)}]


def sse(ev, data):
    return ("event: %s\ndata: %s\n\n" % (ev, json.dumps(data))).encode()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.json({})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        if "count_tokens" in self.path:
            return self.json({"input_tokens": 10})
        msgs = body.get("messages", [])
        results = [b for m in msgs if m.get("role") == "user" for b in blocks(m["content"])
                   if isinstance(b, dict) and b.get("type") == "tool_result"]
        main = bool(body.get("tools")) and body.get("max_tokens", 0) > 1
        if main and len(results) < len(SCRIPT):
            content = [{"type": "tool_use", "id": "toolu_mock%02d" % len(results), "name": "Bash", "input": SCRIPT[len(results)]}]
            stop = "tool_use"
        elif main:
            last = results[-1]["content"] if results else ""
            last = json.dumps(last) if not isinstance(last, str) else last
            content = [{"type": "text", "text": "Last Bash call result: " + last[:1500]}]
            stop = "end_turn"
        else:
            content = [{"type": "text", "text": "ok"}]
            stop = "end_turn"
        msg = {"id": "msg_mock%d" % int(time.time() * 1000), "type": "message", "role": "assistant", "model": body.get("model", "mock"),
               "content": content, "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10}}
        if not body.get("stream"):
            return self.json(msg)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        w = self.wfile.write
        w(sse("message_start", {"type": "message_start", "message": dict(msg, content=[], stop_reason=None)}))
        for i, b in enumerate(content):
            if b["type"] == "tool_use":
                w(sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": dict(b, input={})}))
                w(sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                              "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}}))
            else:
                w(sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}))
                w(sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}}))
            w(sse("content_block_stop", {"type": "content_block_stop", "index": i}))
        w(sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 10}}))
        w(sse("message_stop", {"type": "message_stop"}))

    def json(self, o):
        b = json.dumps(o).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()

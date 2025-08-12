from flask import Flask, jsonify
import os, socket, datetime as dt

app = Flask(__name__)

@app.get("/")
def index():
    return f"OK from {socket.gethostname()} at {dt.datetime.utcnow().isoformat()}Z"

@app.get("/health")
def health():
    return jsonify(status="healthy", port=os.environ.get("PORT"))

if __name__ == "__main__":
    # برای اجرای لوکال
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


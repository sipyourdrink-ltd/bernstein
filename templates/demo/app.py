"""Simple Flask web application for the Bernstein demo."""

from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/")
def hello() -> object:
    """Return a greeting."""
    return jsonify({"message": "Hello, World!", "status": "ok"})


if __name__ == "__main__":
    app.run(debug=True)

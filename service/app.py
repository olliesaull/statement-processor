import os

from flask import Flask

from configuration.config import CLIENT_ID, CLIENT_SECRET
from routes import register_routes

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

app.config["CLIENT_ID"] = CLIENT_ID
app.config["CLIENT_SECRET"] = CLIENT_SECRET

register_routes(app)

if __name__ == "__main__":
    app.run(port=8080, debug=True)

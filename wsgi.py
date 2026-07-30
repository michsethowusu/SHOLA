"""Entry point. `flask --app wsgi run` in development, gunicorn in production."""

from dotenv import load_dotenv

load_dotenv()

from shola import create_app  # noqa: E402  (import after .env is loaded)

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

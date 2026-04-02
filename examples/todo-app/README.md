# Example TODO App

A Flask-based TODO API built with Bernstein.

## Quick Start

```bash
cd examples/todo-app
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
python app.py
```

## API Endpoints

- `GET /todos` - List all todos
- `POST /todos` - Create a todo
- `GET /todos/<id>` - Get a todo
- `PUT /todos/<id>` - Update a todo
- `DELETE /todos/<id>` - Delete a todo

## Running with Bernstein

```bash
bernstein run --goal "Add user authentication to the TODO app"
```

## Project Structure

```
todo-app/
├── app.py          # Flask application
├── models.py       # Data models
├── tests/          # Unit tests
└── bernstein.yaml  # Bernstein configuration
```

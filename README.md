# Garage Control

A local Flask garage build planner that stores cars, budgets, parts, users, collaborators, and change history in a SQLite file database.

## Local development

```bash
python app.py
```

## Future private sharing path

The current app is designed for private local development with SQLite. When the project is moved into a future private shared web deployment, the safest progression is:

1. Keep the Flask app as the source of truth.
2. Move the SQLite database to a private service or managed database rather than a personal OneDrive folder.
3. Keep users separate from the app and require strong environment secrets.
4. Serve over HTTPS with a production WSGI server.
5. Store password hashes using Werkzeug and keep the file system outside of a public web directory.

Do not make the local database public or expose the development server.

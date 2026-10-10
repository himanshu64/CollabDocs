# CollabDocs

Backend API for a collaborative document platform: workspaces, members with roles, versioned documents, threaded comments, tags and an audit log. Built with Django REST Framework and PostgreSQL. API-only; use Postman as the client.

**Demo video:** _add Loom / Google Drive link here_

## Setup

Requires Python 3.12+ and PostgreSQL.

```bash
git clone <repo-url> && cd CollabDocs
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then fill in your DB credentials and a SECRET_KEY
createdb collabdocs           # or create the database named in DB_NAME
```

## Apply migrations

```bash
python manage.py migrate
```

## Run the server

```bash
python manage.py runserver
```

The API is served at `http://localhost:8000/api/`. Every request is logged to the console by the custom middleware:

```
POST /api/documents/ 201 34.12ms
```

## Run the tests

```bash
python manage.py test core
```

## Postman

Import `CollabDocs.postman_collection.json` from the repo root. Requests are grouped into the folders Users, Workspaces, Tags, Documents, Comments and Audit Logs. Each create request saves the returned `id` into a collection variable (`user_id`, `workspace_id`, `document_id`, ...), so running the collection top to bottom works without copy-pasting IDs. `base_url` defaults to `http://localhost:8000`.

There is no authentication: the brief's User model has no password, so the acting user is passed by ID (`owner`, `created_by`, `author`).

## Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/users/` | Create a user |
| GET | `/api/users/{id}/` | Get user by ID |
| POST | `/api/workspaces/` | Create workspace; owner is added as admin in the same transaction. Optional `members` list is added atomically too |
| GET | `/api/workspaces/{id}/` | Workspace with `member_count` (annotate + Count) |
| POST | `/api/workspaces/{id}/members/` | Add a member with a role (404 unknown user, 409 duplicate) |
| GET | `/api/workspaces/{id}/members/` | List members and roles |
| GET | `/api/workspaces/{id}/summary/` | Document count, member count, total comments, documents by status |
| POST | `/api/documents/` | Create document + version 1 (atomic) |
| PUT | `/api/documents/{id}/` | Update document, saving a new version (atomic) |
| GET | `/api/documents/` | List documents. Filters: `workspace`, `status` (comma-separated), `tag`, `search` (title OR content), `updated_after`, `updated_before` |
| GET | `/api/documents/{id}/versions/` | All versions in order |
| GET | `/api/documents/{id}/stats/` | Version count, comment count, contributor count |
| POST | `/api/documents/{id}/tags/` | Add tags: `{"tag_ids": ["<uuid>", ...]}` |
| POST | `/api/comments/` | Add a top-level comment or a reply (`parent`) |
| GET | `/api/comments/?document={id}` | Threaded comments for a document |
| POST | `/api/tags/` | Create a tag (409 if the name exists) |
| GET | `/api/audit-logs/` | Audit logs. Filters: `actor`, `action`, `model_name`, `from`, `to` (YYYY-MM-DD) |

## How the integrity requirements are met

- **Workspace creation**: workspace, owner membership (role `admin`) and any extra `members` are created inside one `transaction.atomic()`. A duplicate member violates the `unique_workspace_member` constraint, the `IntegrityError` is caught, the response is `409`, and nothing is saved.
- **Document saves**: `create()` and `update()` wrap the save, the new `DocumentVersion` (`version_number = document.versions.count() + 1`) and the `AuditLog` written by the `post_save` signal in one atomic block. Updates lock the document row first so concurrent saves can't get the same version number.
- **Audit signal** (`core/signals.py`, connected in `CoreConfig.ready()`): Django sets `_state.adding` to `False` before `post_save` fires, so a `pre_save` receiver records `instance._state.adding` and the `post_save` receiver uses it to log `created` or `updated`.

## Demo walkthrough

1. Run the Users and Workspaces folders and watch the middleware lines in the server console.
2. **Rollback:** run "Rollback demo: duplicate member -> 409, nothing saved", then "List workspaces (filter by name)", which returns `[]`.
3. **Aggregations:** "Workspace summary" and "Document stats".
4. **Signal:** run "Update document (new version)", then "List audit logs" to see the `updated` entry for the document.

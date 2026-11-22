# Contributing

Issues and pull requests are welcome. This is a personal project, so replies can be slow.

1. Open an issue first for anything bigger than a typo, so we agree on the change before you write it.
2. Set the project up as described in the [README](README.md#local-setup).
3. Run the checks before you push (commands in the README): backend `ruff check`, `ruff format --check`, `mypy` and `pytest` (pytest needs Docker); web `npm run lint`, `npm run typecheck`, `npm test` and `npm run build`.
4. Keep a pull request small and about one topic. Add or update a test for behaviour you change, and write the test so it fails before your fix.
5. A change to the database schema (Alembic migration) or to the API shape (paths, response fields, error codes) needs to be discussed in an issue first.
6. Never commit secrets. `.env` is ignored. Add only new variable names, without values, to `.env.example`, and only names the code reads.
7. Commit messages: one short line.

Pull requests from forks run CI with a read-only token and no access to secrets, and the first run may need a maintainer's approval before it starts.

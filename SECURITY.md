# Security policy

QueueLess is a portfolio project, not a production service. It has no hosted deployment today. Sign-in codes are never sent by SMS: the only provider is a mock that writes the code to the API log in local and test runs. Please do not enter real personal data into any copy of it, including any demo that may be hosted later.

## Reporting a vulnerability

Please report a security problem privately, not in a public issue.

Use GitHub's private vulnerability reporting: on the repository page open the **Security** tab and choose **Report a vulnerability**. If that option is not shown, open a public issue that says only that you have a security report, with no details, and I will arrange a private channel.

This is a one-person project: expect a first reply within about 7 days. There is no bug bounty.

## Scope

In scope: the API under `backend/`, the web app under `web/`, and the Docker Compose files.

Out of scope: volume-based denial of service, third-party platforms and services, and the local-only placeholder secrets in `docker-compose.yml`. Those placeholders are public on purpose, and the API refuses to start with them unless `APP_ENV=local`.

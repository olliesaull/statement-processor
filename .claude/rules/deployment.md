---
paths:
  - "service/routes/**/*.py"
  - "service/Dockerfile"
  - "service/nginx*"
  - "service/app.py"
  - "service/utils/auth.py"
---

# Deployment Configuration Checklist

There is no nginx in the local dev environment, so changes that affect nginx or the Docker image will work locally but **break in production** if the config files are not updated. Always check these when making changes:

## Dockerfile (`service/Dockerfile`)
- **Build context is the repo root**, not `service/`. All COPY paths must be prefixed with `service/` (e.g. `COPY service/routes/ ./routes/`). The same applies to Lambda Dockerfiles (prefix with `lambda_functions/<name>/`). This is because the shared `common/` package lives at the repo root and all images need access to it.
- **New directories under `service/`**: Add a `COPY service/<dir>/ ./<dir>/` line. The Dockerfile copies directories explicitly — new ones are silently excluded from the container image.
- **New config/data files**: If the app reads a new file at runtime, ensure it is copied into the image.
- **`.dockerignore`** at the repo root controls what enters the build context — keep it up to date when adding large directories.

## Nginx query string allowlist (`service/nginx_route_querystring_allow_list.json`)
- **Adding or renaming query parameters on a route**: Add/update the entry in this JSON file. Public routes have query strings **stripped** by nginx unless explicitly allowed here. This is the most common production-only failure — the app works locally because there is no nginx, but 404s in production because the parameter is blocked.

## Nginx route regeneration (`service/nginx-routes.conf`)
- **Adding/removing Flask routes, changing auth decorators, or changing allowed query params**: Regenerate `nginx-routes.conf` by running the generator from `service/`:
  ```
  cd service && python3.13 nginx_route_config_generator.py
  ```
  Review the diff before committing.

## Nginx route overrides (`service/nginx_route_overrides.json`)
- **Routes needing non-default body size or timeout**: Add an entry here (e.g. `client_max_body_size`, `proxy_read_timeout`).

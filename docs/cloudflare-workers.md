# Cloudflare Workers deployment

The repository has two independent Workers. The Django API and database remain
the application origin on Railway; `project-x` is the fixed-origin edge API
proxy. `backendmoderation` is a scheduled Worker that leases moderation jobs
from Django, fetches media through Cloudflare Media Transformations, calls
Workers AI, and submits raw frame results back to Django. Django keeps the
moderation policy and all database writes.

## Cloudflare Workers Builds settings

Configure each Worker’s Git integration separately:

| Worker | Root directory | Build command | Deploy command |
| --- | --- | --- | --- |
| `project-x` | `workers/project-x` | leave empty | `npx wrangler deploy` |
| `backendmoderation` | `workers/backendmoderation` | leave empty | `npx wrangler deploy` |

The previous builds ran from the repository root. With no `wrangler.toml` or
Worker entrypoint there, Wrangler fell back to its static-project detection and
reported that it could not find static files.

## Runtime variables and secrets

Set these in each Worker’s production Variables and Secrets. Never commit the
values:

`project-x` needs `BACKEND_ORIGIN` (the checked-in default points at the current
Railway origin and can be overridden in Cloudflare).

`backendmoderation` needs `DJANGO_ORIGIN`, `MODERATION_WORKER_TOKEN`,
`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, and
`CLOUDFLARE_MEDIA_TRANSFORM_BASE`. `MODERATION_ADMIN_TOKEN` is required only if
the manual `POST /run` endpoint is used. The Cloudflare API token must be
allowed to call Workers AI; it is separate from the user-scoped token used by
the Workers Builds management API.

Create one long random `MODERATION_WORKER_TOKEN` and set the same value on the
Railway `project-x` service and on this Worker. The Django endpoints reject all
leases and settlements while that API variable is empty. If the Railway Go
moderation service remains enabled during the cutover, give it the same value
as well; otherwise pause that service after the Cloudflare Worker is verified.

After saving the root directories and variables, use **Retry build** for each
failed deployment. Then verify `GET /healthz` on the moderation Worker and a
normal API request through the `project-x` Worker before switching the mobile
client’s API URL.

## Mobile client

The Expo app already reads `EXPO_PUBLIC_API_URL` for both HTTP requests and
the chat WebSocket. Once the `project-x` Worker has a public hostname, set that
value in the mobile app's EAS profile (or in the local `.env.local` used for a
development build) and rebuild the JavaScript bundle. No native rebuild is
needed for this URL-only change.

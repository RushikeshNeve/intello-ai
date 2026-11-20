# Free-VM cost and limit sheet (M10 preparation)

Written from general knowledge, NOT from a live check of any provider's current terms. Treat every line marked "verify" as unknown until you read the provider's page at provisioning time. No account has been created.

## Components and where cost can come from
| Item | Planned source | Cost | Limits / what can go wrong |
|---|---|---|---|
| VM (4 vCPU, 24 GB, arm64) | Oracle Cloud Always Free Ampere A1 (verify the current allowance) | $0 within the free allowance | Capacity shortages in popular regions; accounts that look inactive or unverified can be restricted; idle instances may be reclaimed (verify the current idle policy); upgrading to a paid account removes the "free" guard-rails but keeps free allowances (verify) |
| Block storage | included boot volume (about 200 GB total free allowance, verify) | $0 | Backups on the same disk are not real backups |
| Public IPv4 | reserved public IP | $0 while attached (verify) | An unattached reserved IP can bill; an ephemeral IP can change |
| Outbound traffic | free allowance (about 10 TB/month, verify) | $0 | far above what this app needs |
| Domain | DuckDNS subdomains (two needed) or an owned domain | $0 / about $10-15 per year | DuckDNS is a free community service: availability and the name limit are not guaranteed (verify); owned domain is more robust |
| TLS | Let's Encrypt via Caddy | $0 | Rate limits on repeated failed issuance; needs 80/443 reachable and correct DNS |
| AI provider (chat + embeddings) | OpenAI-compatible hosted API | PAID per token, not free | Set a hard monthly budget in the provider console before first use; a leaked key or an abusive user can spend real money; free-tier APIs exist but have request/day limits and policy limits (verify) |
| Off-VM backup storage | Cloudflare R2 / Backblaze B2 / Google Drive via rclone | about $0 under about 10 GB (verify) | Free quota exhaustion; keep retention small |
| Monitoring | none beyond container logs and the smoke script | $0 | No alerting: you find out when you look. Optional: an external uptime pinger on `/api/health` (free tiers exist, verify) |

## What can pause, expire or disappear
- Free VM reclaimed or terminated after inactivity or an account problem -> total loss of the single VM. Mitigation: off-VM backups, tested restore.
- Region capacity: you may not get the 4 vCPU / 24 GB shape at all; the stack needs roughly 8-10 GB RAM at minimum (limits in `prod.yml` sum to about 20 GB at peak, they are ceilings).
- DNS name lost (DuckDNS) -> TLS renewal fails after ~60 days.
- AI key revoked / budget hit -> ingestion (embeddings) and chat fail; uploads still store.
- Disk full from logs/images/backups -> Postgres stops. Logs are capped (10 MB x 5 per container); prune old images; keep BACKUP_KEEP small.

## Honest uncertainties to verify at provisioning time
1. Current Oracle Always Free limits (OCPU, RAM, storage, idle-reclamation rule, whether a card is required, whether the home region can be changed later).
2. That arm64 builds of the API image succeed and RushDB/RustFS run on arm64 (images are multi-arch; runtime unverified).
3. DuckDNS limit on names per account and uptime history; whether you prefer an owned domain.
4. The chosen AI provider's pricing, rate limits, data-use policy, and whether it fits the tenant AI policy (`AI_EXTERNAL_PROVIDERS_ENABLED`).
5. Whether RushDB's license/terms allow your intended public use (see ADR-012 / RushDB validation spike).
6. Security: single VM, single admin, no WAF; SSH locked to your IP and key-only is on you.

## Expected monthly cost (rough)
$0 infrastructure if everything above holds; plus AI usage (the only variable cost; budget-cap it); plus about $1/month if you buy a domain.

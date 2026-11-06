# Recertia Specifications: OpenAI-compatible gateways (OpenRouter)

Normative requirements for using OpenRouter and other Chat Completions gateways through
Recertia’s OpenAI client. Architecture:
[`../architecture/openai-compat-gateways.md`](../architecture/openai-compat-gateways.md).
Decision: [ADR-0013](../adr/0013-openai-compat-gateways.md).

These rules extend — and do not replace — model/cost behaviour described in
[`promotion-api-and-observability.md`](promotion-api-and-observability.md) and the go-live
operator guide.

## 1. Provider model

1. Recertia MUST expose at most these solver provider names for configuration:
   `stub`, `anthropic`, `openai`.
2. OpenRouter and peers MUST be configured as `openai` plus a gateway base URL. Recertia
   MUST NOT require a distinct `openrouter` provider enum for gateways that speak Chat
   Completions.
3. Run manifests and spend records MUST label `provider` as `openai` when this path is
   used. The gateway model slug MUST appear in `model_id`.
4. CLI `--model openai:<slug>` MUST accept slugs containing `/` (single `:` partition).

## 2. Endpoint and authentication

1. When `RECERTIA_OPENAI_BASE_URL` is set, `OpenAIModelClient` MUST POST to that value
   **verbatim**. The value MUST be a full Chat Completions URL (including path). Recertia
   MUST NOT append `/chat/completions` itself.
2. Default URL when unset: `https://api.openai.com/v1/chat/completions`.
3. Requests MUST use `Authorization: Bearer <api_key>` where `<api_key>` is resolved from
   `OPENAI_API_KEY` or the env named by `RECERTIA_API_KEY_ENV`.
4. OpenRouter keys (`sk-or-…`) are valid Bearer tokens on this surface. Recertia MUST NOT
   require a separate `OPENROUTER_API_KEY` env for the OR0 path (aliases MAY be added later
   without breaking `OPENAI_API_KEY`).

## 3. Headers (gateway metadata)

| Env | Header(s) | Required for API success |
| --- | --- | --- |
| `RECERTIA_OPENAI_HTTP_REFERER` | `HTTP-Referer` | No (OpenRouter rankings / attribution) |
| `RECERTIA_OPENAI_TITLE` or `RECERTIA_OPENROUTER_TITLE` | `X-OpenRouter-Title` and `X-Title` | No |
| `RECERTIA_OPENAI_EXTRA_HEADERS` | caller-defined string map | No |

1. `RECERTIA_OPENAI_EXTRA_HEADERS` MUST be a JSON object whose values are coerced to
   strings. Invalid JSON MUST fail the request with a provider error (fail loud).
2. Attribution headers SHOULD be set for public OpenRouter apps; they MUST NOT be treated
   as authentication.

## 4. Request body

1. The client MUST send at least `model` and `messages` (optional leading `system` message
   plus one `user` message).
2. `RECERTIA_OPENAI_EXTRA_BODY` MUST be a JSON object merged into the body.
3. Extra body MUST NOT override `model` or `messages` (those keys MUST be stripped if
   present).
4. Generation controls (`max_tokens`, `temperature`, OpenRouter `provider` routing object,
   etc.) MUST be supplied via `RECERTIA_OPENAI_EXTRA_BODY` when needed. The OpenAI client
   MUST NOT invent Anthropic-style default `max_tokens` unless configured.
5. Invalid `EXTRA_BODY` JSON MUST fail loud.

## 5. Response handling

1. Text MUST be taken from `choices[0].message.content` when that field is a non-empty
   string.
2. Missing `choices`, missing `message`, or empty/non-string content MUST raise a provider
   error.
3. Token usage MUST prefer `usage.prompt_tokens` and `usage.completion_tokens`; when absent,
   clients MAY estimate from character length for cost accounting continuity.
4. Multimodal / array `content` shapes are out of scope for OR0–OR1; support requires an
   explicit milestone.

## 6. Cost accounting

1. Cost MUST be computed with `estimate_cost_usd(provider="openai", model_id=<slug>, …)`.
2. Built-in rate tables cover common Anthropic/OpenAI ids. Gateway slugs without a table
   match MUST fall back to
   `RECERTIA_DEFAULT_INPUT_USD_PER_MTOK` / `RECERTIA_DEFAULT_OUTPUT_USD_PER_MTOK`
   (defaults 1.0 / 3.0) unless
   `RECERTIA_MODEL_PRICE_OPENAI_<SLUG>_IN` / `_OUT` are set
   (slug: non-alphanumerics → `_`, uppercased).
3. Operators using OpenRouter for production spend reporting MUST set per-slug price
   overrides or accept that defaults are approximate. Metrics/docs MUST NOT present
   defaulted gateway spend as vendor-exact.

## 7. Verifier and identity

1. Verifier MAY share the OpenAI/OpenRouter client class and credential env.
2. Prefer a distinct `RECERTIA_VERIFIER_MODEL_ID` (different OpenRouter slug) so
   `shares_identity_with` does not collapse solver and judge.
3. Distinct credentials remain a documented preference; OR0 does not require a second key
   env name.

## 8. Console and API

1. HTTP `POST /v1/runs` and the product console MUST inherit process-level model env; they
   MUST NOT accept raw OpenRouter API keys in browser-visible JSON for routine runs.
2. Console SSE (`GET /v1/runs/{id}/events`) streams **run** events. It MUST NOT be
   confused with provider token streaming.
3. A console model picker (if added) MUST only select allowlisted provider/slug pairs
   resolved server-side — never paste keys into SPA storage. `GET /v1/models` is the
   allowlist source; `POST /v1/runs` `model` is optional and fail-closed against
   `RECERTIA_MODEL_ALLOWLIST` / `RECERTIA_MODEL_ALLOWLIST_PATH` when present.

## 9. Conformance tests (CI)

| ID | Assertion |
| --- | --- |
| OG-1 | With gateway env set, outbound request URL equals `RECERTIA_OPENAI_BASE_URL` |
| OG-2 | `HTTP-Referer`, `X-OpenRouter-Title`, and `X-Title` are sent when title/referer env set |
| OG-3 | `EXTRA_BODY` merges fields and cannot override `model` / `messages` |
| OG-4 | `EXTRA_HEADERS` invalid JSON fails loud |
| OG-5 | CLI/config accepts `openai:org/model` slugs with `/` |
| OG-6 | Price override env for a slash slug affects `estimate_cost_usd` |
| OG-7 | Unknown gateway slugs are not vendor-exact; `cost_is_vendor_exact` is false |
| OG-8 | `RECERTIA_OPENAI_MAX_TOKENS` is sent unless EXTRA_BODY already has `max_tokens` |
| OG-9 | OpenRouter `{error:{message,code}}` raises `ProviderError` including `code=` |
| OG-10 | List-shaped `message.content` concatenates `{type:text,text}` parts |
| OG-11 | `POST /v1/runs` with a console-selected slug not on the server allowlist returns 400; allowlist is not in `src/recertia/console/static/`; `GET /v1/models` returns the allowlist |

Existing coverage: `tests/unit/test_openai_compat_gateway.py` (OR0–OR3). OG-11 lives with those tests.

## 10. Explicit non-requirements

- OpenRouter SDK or Anthropic-via-OpenRouter dual protocol
- Provider preference UI without server allowlists
- Exactly-once cross-region failover across model hosts
- Claiming research assumptions supported because a gateway was configured

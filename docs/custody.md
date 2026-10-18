# Custody: who sees client pixels, and on what terms

Status 2026-10-17 (day 1 of the scaffold). This is the human-readable side of the `custody_grade` and `custody_status` fields on every route in `engines/plate_engines/manifests/*.yaml`. The router reads the manifests, not this file. If the two disagree, fix the manifest and add a line to the change log at the bottom. Custody classes are a Fable decision under the house routing and every grade change is approved by Aasish.

**How to read the evidence.** Sthyra holds no written retention, training, region or deletion terms from any cloud vendor today. No vendor terms page was fetched for this pass. A vendor fact appears below only if one of the local records in section 2.1 states it, and it carries that record's `Source:` line. Every other cell says `verify in writing` and section 3 gives the exact question and who to ask. One record is newer: E19 (Runway) was read from the vendor's public terms pages on 2026-10-18, which is why the Runway Dev API row is graded on the evidence. The dossier's notes on vendor terms (VIZMAKER.md section 9) are secondary research written about VizMaker's routes, so they shape the questions but never move a grade.

## 1. Grades

Decision 9, verbatim:

> **Custody grades.** C0 local only. C1 first-party API with written no-training and zero-retention terms (OpenAI with ZDR approval, Google Vertex or Gemini paid tier; request both in writing on day 1). C2 a named reseller whose terms we hold (Atlas once its retention and upload-URL terms arrive). D unknown terms or public media (Atlas today, because its upload endpoint returns public Alibaba OSS URLs; fal; Higgsfield; Runway). NDA projects run on C0 and C1 only; there is no C1 video path today, so NDA video means local Wan 2.2 previs or written client consent. The Claude Code and MCP agent channel is an egress route too: NDA work uses an API key under commercial terms, never the consumer login.

Source: ~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md, section 3, decision 9 (read 2026-10-17)

| grade | what it means | NDA project |
|---|---|---|
| C0 | Local only: the render box and Sthyra machines. Nothing leaves. | allowed |
| C1 | First-party API, and Sthyra holds written no-training **and** zero-retention terms. | allowed |
| C2 | A named reseller, and Sthyra holds its terms. | not allowed |
| D | Terms unknown or not held, or the media becomes public. | not allowed |

**Pending counts as D.** Every route carries `custody_grade` (C0, C1, C2, D) and `custody_status` (`confirmed` or `pending_written_terms`). The router's `effective_grade` is the grade when `confirmed` and D otherwise, and the custody filter runs before anything else (plan decision 4, task T5 `plate_engines/router.py`). So the OpenAI, Gemini, Vertex and Anthropic API routes are written as C1 candidates and behave as D until the written answers are filed. A vendor's reputation does not change this.

**NDA rule.** NDA projects run on C0 and C1 only. There is no C1 video path today, so NDA video means local Wan 2.2 VACE previs, a V-Ray or Vantage render, or the client's written consent naming the vendor (see `docs/consent-template.md`). A confirmed Veo 3.1 route on Vertex would be the first C1 video path.

**Promotion rule (pending to confirmed).** A route moves only when all of these hold: (1) the answer is in writing from the vendor (signed terms, a DPA or addendum, or an email from an address the vendor confirms is authorised to bind it); (2) it answers every question for that vendor in section 3 that its target grade depends on; (3) the document is filed with Sthyra's contract records and its sha256 goes into the route's `terms_hash`; (4) Aasish approves and the change log below records the date, route and document hash. If a vendor later changes its terms, the route returns to `pending_written_terms` until the new text is read.

## 2. Matrix

| Vendor (route) | Grade today | Grade achievable | Upload visibility | Retention | Training on inputs | Region | Sub-processors known? | NDA allowed today | Evidence | Action |
|---|---|---|---|---|---|---|---|---|---|---|
| **Atlas (AtlasCloud)**, reseller for Seedream 5 Pro, Seedance 2.5, Kling O3 4K and v3 Pro | **D** (on the evidence, not pending) | C2, once retention and private-upload terms arrive in writing | **Public**: upload returns a `download_url` on Alibaba OSS; results are fetched with no credentials from `.aliyuncs.com` / `.atlascloud.ai` hosts | verify in writing | verify in writing (Atlas and each upstream); upstream Kling has a product-development clause | verify in writing; files sit on Alibaba OSS hosts; Kling's operator stores data in Singapore | No. Model makers known by name only (ByteDance, Kuaishou, Google); Atlas's actual upstream routing: verify in writing | **No** (router refuses, code `custody`) | E1, E2, E3, E4, E5, E6 | Email 3.3; retroactive inventory and deletion request (section 4) |
| **Runway (Dev API)** (adapter: Seedance 2.5, Kling O3 4K and 3.0 Pro, Veo 3.1, Gen-4.5) | **D** (on the evidence: self-serve Terms of Use 15 Sep 2026 s4.4 grants a perpetual training licence on Inputs and Outputs) | C2 under a signed Runway Dev Enterprise agreement (no training, zero retention, DPA); C1 is not reachable (Runway is not the first party for these models) | Private: data URIs inside the request or 24 h ephemeral runway:// uploads; outputs on signed URLs that expire in 24–48 h | self-serve: unpublished; task delete removes outputs; Enterprise: zero retention | Yes on self-serve (s4.4); no under Enterprise | US (New York; AWS/GCP); Seedance and Kling calls are processed by Byteplus Pte and Kling AI Pte in Singapore (inferred from the sub-processor list; verify in writing) | Partly: Google Cloud, Byteplus Pte, Kling AI Pte, Anthropic, Hive, Databricks on the public list; model-to-sub-processor map not published | **No** | E1, E2, E7, E19 | Email 3.5 (now also: an Enterprise quote); deletion request for past uploads (section 4) |
| **Runway (web app)** (web app; no adapter; graded apart from the Dev API row above) | **D** | C2 for a resold model, or C1 for a first-party API with written no-training and zero-retention terms; which applies: verify in writing | verify in writing | verify in writing | verify in writing | verify in writing | No | **No** | E1, E2, E7 | Email 3.5; deletion request for past uploads (section 4) |
| **Higgsfield** (web app and CLI; no adapter) | **D** | C2 for resold models (it is named as a multi-engine substitute), C1 only for a first-party model with written terms; verify in writing | verify in writing | verify in writing | verify in writing | verify in writing | No; which upstream makers receive inputs: verify in writing | **No** | E1, E2, E7, E8 | Email 3.6; deletion request for past uploads (section 4) |
| **fal** (not in the v1 roster; no manifest route) | **D** | C2 for hosted third-party models once terms are held; C1 only if fal is the first party for a model and gives written no-training and zero-retention terms | verify in writing | verify in writing | verify in writing | verify in writing | No | **No** | E1, E12 | Email 3.7 before any adapter is written |
| **OpenAI** (image API: GPT Image 2.5, direct) | **D** effective (manifest: C1, `pending_written_terms`) | C1, with ZDR approval covering the image endpoints | verify in writing (no adapter yet) | verify in writing; C1 needs ZDR approval for the image endpoints | Dossier says the API does not train by default; not held in writing for Sthyra's organisation: verify in writing | verify in writing | No | **No** (pending) | E1, E4, E10, E11, E12 | Email 3.2 |
| **Google Gemini API** (paid tier: Nano Banana 2.1 and Pro, direct) | **D** effective (manifest: C1, `pending_written_terms`) | C1 | verify in writing (no adapter yet) | verify in writing | Dossier's statement covers EEA developers only; Sthyra's position as an Indian developer: verify in writing | Dossier says paid terms allow transient storage in any country where Google has facilities, so no region guarantee: verify in writing | No | **No** (pending) | E1, E4, E10, E11, E12 | Email 3.1 |
| **Google Vertex AI** (Veo 3.1; Nano Banana if offered on Vertex) | **D** effective (manifest: C1, `pending_written_terms`) | C1, which would be the first C1 video path | verify in writing (no adapter yet) | zero-retention: verify in writing | verify in writing | `asia-south1` availability for Nano Banana and Veo: verify in writing | No | **No** (pending) | E1, E4, E10, E12 | Email 3.1 |
| **Anthropic API** (API key under commercial terms: Sonnet 5.5 composer, Opus 5.5 judge, Claude Code and MCP agent sessions run on an API key) | **D** effective (C1 candidate, pending) | C1 | verify in writing | verify in writing | verify in writing | verify in writing | No | **No** (pending): until confirmed, NDA projects run without the semantic judge and no agent session reads NDA material | E1, E13, E15 | Email 3.4; record the route's custody fields where the composer and judge are configured |
| **Anthropic consumer login** (a Claude subscription login in Claude Code or the Claude apps) | **D** for client data | D (no path; use an API key instead) | not a route for client data | not a route for client data | not a route for client data | not a route for client data | not a route for client data | **Never** | E1 | Check how a Claude Code session is signed in (`/status`) before it reads any client plate, derivative or packet |

A note on grade today for Atlas: its D is not a pending candidate. The public upload URL is an observed fact, so the route is D on the evidence and stays D until the written terms say uploads can be private.

### 2.1 Evidence

- **E1.** Decision 9: the grade definitions; Atlas D because its upload endpoint returns public Alibaba OSS URLs; fal, Higgsfield and Runway D; OpenAI with ZDR approval and Google Vertex or Gemini paid tier as the C1 candidates; NDA on C0 and C1 only; no C1 video path today; the agent channel uses an API key under commercial terms, never the consumer login. Source: ~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md, section 3, decision 9 (read 2026-10-17)
- **E2.** "Client plates have already gone to Atlas, Higgsfield and Runway, so this is overdue, not future work." Source: ~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md, section 3, decision 10 (read 2026-10-17)
- **E3.** STAFFAGE's Atlas client: `atlasUpload` POSTs `/model/uploadMedia` and returns `data.download_url` (lines 17 to 29); `atlasDownload` fetches a result URL with a plain `fetch(url)` and no Authorization header, after checking the host ends in `.aliyuncs.com` or `.atlascloud.ai` (lines 9 and 63 to 66). The code does not show how long either URL lives or whether it is guessable. Source: ~/Desktop/staffage/src/lib/atlas.ts (read 2026-10-17)
- **E4.** Day-1 questions: Google (Vertex zero-retention, Nano Banana regional availability, `asia-south1`); OpenAI (ZDR eligibility for image endpoints); Atlas (retention, whether upload URLs can be private, the discount semantics, upstream routing). "Until the answers arrive, Atlas is grade D and NDA video has no cloud path." Source: ~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md, section 8 (read 2026-10-17)
- **E5.** "Kling in Singapore with a product-development clause". Source: ~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md, section 7, "Data custody" (read 2026-10-17). Same point: "Kling's operator stores data in Singapore and may use it for product development". Source: ~/Desktop/test/vizmaker-dossier/VIZMAKER.md, section 9 (read 2026-10-17)
- **E6.** Model makers: Seedream and Seedance are ByteDance, Kling is Kuaishou, Nano Banana and Veo are Google, GPT Image is OpenAI. Source: ~/Desktop/test/vizmaker-dossier/VIZMAKER.md, section 5 table (read 2026-10-17)
- **E7.** Higgsfield and Runway web UIs are the manual escape hatch: any file they generate is imported, gated and ledgered so a manual route never bypasses the audit trail. Source: ~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md, section 7, "Delivery-day outages" (read 2026-10-17)
- **E8.** Commenters name "Higgsfield (cheaper multi-engine)" as a substitute for VizMaker. Source: ~/Desktop/test/vizmaker-dossier/VIZMAKER.md, section 11 (read 2026-10-17)
- **E9.** Custody status is data and anything not `confirmed` is D; router order is custody, then doctrine, then health and price; a pinned engine that fails custody refuses with `code="custody"`, never a silent fallback. Source: .router/PLAN.md, decision 4 and task T5 (read 2026-10-17)
- **E10.** Manifests written in wave 1: `gpt-image-2.5` (OpenAI direct, C1 pending, note ZDR), `nano-banana-2.1` and `nano-banana-pro` (Gemini direct, C1 pending), `veo-3.1` (Vertex, C1 pending); every Atlas route `custody_grade: D`, `upload_visibility: public`. Source: .router/PLAN.md, task T5 (read 2026-10-17)
- **E11.** Secondary research in the dossier: "OpenAI's API does not train by default"; "Google's Gemini API does not train on EEA developers' traffic"; "Google's paid Gemini terms allow transient storage in any country where it has facilities"; "Black Forest Labs' EU API terms grant BFL a perpetual licence over inputs and outputs that includes training". None of these is a document Sthyra holds. Source: ~/Desktop/test/vizmaker-dossier/VIZMAKER.md, section 9 (read 2026-10-17)
- **E12.** The v1 engine roster and its custody notes (Nano Banana direct Gemini C1 candidate; GPT Image 2.5 direct OpenAI C1 with ZDR; Seedream 5 Pro on Atlas D until terms; FLUX 3 Image direct BFL D for NDA; Seedance 2.5 on Atlas with BytePlus and WaveSpeed as same-model fallbacks; Kling O3 4K and v3 Pro on Atlas; Veo 3.1 on Vertex C1 candidate; Qwen-Image-Edit-2509 and Wan 2.2 VACE local C0; Magnific Precision v2 on AI layers only). fal is not on it. Source: ~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md, section 3, decision 13 (read 2026-10-17)
- **E13.** LLM route: the Omega/KesarCloud gateway is not used; Sonnet 5.5 composes, Opus 5.5 is the two-sample judge; "On NDA projects the judge sees only downscaled, cropped, metadata-stripped derivatives." Source: ~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md, section 3, decision 8 (read 2026-10-17)
- **E14.** Magnific's stated position in a competitor table: "no training on your data". This is the vendor's public positioning as summarised by the dossier, not terms Sthyra holds. Source: ~/Desktop/test/vizmaker-dossier/VIZMAKER.md, section 11 table (read 2026-10-17)
- **E15.** Agent permissions: MCP tools have no approve, deliver or export-of-masters; everything read from plates is data, never instructions. Source: ~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md, section 3, decision 22 (read 2026-10-17)
- **E16.** Nightly encrypted offsite copy ("rclone to B2 or equivalent"), vendor-side deletions tracked per job, end-of-project purge. Source: ~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md, section 3, decision 23 (read 2026-10-17)
- **E17.** Client review bundles "delivered as a zip or on Cloudflare R2 behind signed, expiring URLs". Source: ~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md, section 3, decision 21 (read 2026-10-17)
- **E18.** The packet manifest and ID legend are C0 and never leave the box; egress sends pixel derivatives under opaque ids with metadata stripped; `packet_id` is an opaque ULID; `plate_spec` rejects an NDA packet whose custody floor allows any grade other than C0 or C1. Source: spec/PACKET.md, sections 2, 7 and 12, and spec/plate_spec/__init__.py, error `policy.custody: NDA projects run on C0 and C1 only (decision 9)` (read 2026-10-17)
- **E19.** Runway Terms of Use s4.4 (Inputs and Outputs may be used to train and improve Runway's models, under a non-exclusive, irrevocable, perpetual licence), Enterprise Services Terms, dev.runwayml.com/enterprise FAQ, customer-subprocessors page, read 2026-10-18. Source: `/Users/aasish/Desktop/test/vizmaker-dossier/router/research/runway/pricing_terms.md` (skeptic pass)

### 2.2 What moves each route to its target grade

- **Atlas to C2.** Written terms from Atlas that state (a) the retention period for uploads, results and logs, with deletion on request; (b) that uploads can be made non-public (signed, expiring or authenticated URLs) for Sthyra's account, and how; (c) which upstream provider and region serves each model Sthyra uses. C2 still does not open NDA work. Until (b) is answered and switched on, the route is D on the evidence (E3).
- **Runway, Higgsfield, fal to C2 (or C1).** Written retention, training and deletion terms for the product Sthyra actually uses (web app, CLI or API), plus the list of upstream makers each model is passed to. C1 additionally needs the vendor to be the first party for that model and to give written no-training and zero-retention terms. Web-app use is graded separately from any API route unless the same written terms cover both. The Dev API route moves to C2 only under a signed Enterprise agreement; the promotion is a manifest edit (grade C2, status confirmed, terms_hash of the signed agreement) on every `runway` route, nothing else changes.
- **OpenAI to C1.** Written ZDR approval for Sthyra's API organisation that names the image endpoints PLATE calls (GPT Image 2.5 generate and edit), plus written confirmation that inputs and outputs are not used for training.
- **Gemini API to C1.** Written confirmation for Sthyra's paid account that prompts, images and outputs are not used for training and are not retained (or a zero-retention arrangement that covers the image models). The region answer does not change the grade but must be disclosed in the consent form; if Google cannot pin a region, say so there.
- **Vertex to C1.** Written zero-retention and no-training terms for the Vertex generative endpoints PLATE calls (Veo 3.1, and Nano Banana if Vertex serves it). `asia-south1` availability is a region preference for the consent form, not a grade condition.
- **Anthropic API to C1.** Written no-training and zero-retention terms for Sthyra's API organisation, and written confirmation that Claude Code and MCP sessions authenticated with that API key fall under the same terms (including images and tool results inside the session).
- **Anthropic consumer login.** Nothing moves it. Client material goes only through the API key.

### 2.3 Other places client material can go

| Route | Grade today | Note |
|---|---|---|
| Local Qwen-Image-Edit-2509, Wan 2.2 VACE, Real-ESRGAN | C0, `confirmed` | Run on the box; nothing leaves (E12). |
| BFL direct (FLUX 3 Image) | D | Dossier records a training licence in BFL's EU API terms (E11, E12); Sthyra's own terms with BFL: verify in writing. |
| BytePlus and WaveSpeed (Seedance 2.5 same-model fallbacks) | D | Terms not held: verify in writing before either route is used (E12). |
| Magnific Precision v2 | D | AI layers only, never the plate. "No training on your data" is positioning, not held terms (E14): verify in writing. |
| Offsite backup (decision 23) | TO SET | Holds whole projects. Encrypt before upload so the provider holds ciphertext only; provider terms: verify in writing (E16). |
| Client review hosting (decision 21) | TO SET | Proxies, not masters, behind signed expiring URLs; provider terms: verify in writing (E17). |
| Omega/KesarCloud gateway | not a route | Purged on 2026-09-11 and not used (E13). |
| VizMaker | not a route | The trial runs on Sthyra-owned scenes only (decision 24). |

### 2.4 The matrix as the code reads it (generated)

This table is produced from the engine manifests and `custody_routes.yaml`, so it cannot drift from what the router does. The prose matrix above holds the evidence and the actions; this one holds the grades the router acts on. Regenerate it after any manifest or route change:

```
PYTHONPATH=spec:daemon:pixels:engines:gates:plugins/max:tools .venv/bin/python -c "from plate_daemon import custody; print(custody.matrix_markdown())"
```

<!-- custody-matrix:begin (generated by custody.matrix_markdown(); do not edit by hand) -->
| vendor | routes | engines | declared | status | effective | NDA | note |
|---|---|---|---|---|---|---|---|
| Atlas (AtlasCloud) | atlas | kling-o3-4k, kling-v3-pro, nano-banana-2.1, seedance-2.5-i2v, seedance-2.5-r2v, seedream-5-pro | D | confirmed | D | no |  |
| Black Forest Labs | bfl | flux-3-image | D | confirmed | D | no |  |
| BytePlus | byteplus | seedance-2.5-i2v, seedance-2.5-r2v | D | pending_written_terms | D | no | same-model fallback of atlas |
| Google Gemini API | gemini | nano-banana-2.1, nano-banana-pro | C1 | pending_written_terms | D | no | C1 candidate, pending written terms: treated as D |
| Local (render box) | local | qwen-image-edit-2509, real-esrgan, wan-2.2-vace | C0 | confirmed | C0 | yes |  |
| Magnific (Freepik) | magnific | magnific-precision-v2 | D | pending_written_terms | D | no |  |
| OpenAI | openai | gpt-image-2.5 | C1 | pending_written_terms | D | no | C1 candidate, pending written terms: treated as D |
| Runway (Dev API) | runway | gen-4.5, kling-o3-4k, kling-v3-pro, seedance-2.5-i2v, seedance-2.5-r2v, veo-3.1 | D | confirmed | D | no |  |
| Google Vertex AI | vertex | veo-3.1 | C1 | pending_written_terms | D | no | C1 candidate, pending written terms: treated as D |
| WaveSpeed | wavespeed | seedance-2.5-i2v, seedance-2.5-r2v | D | pending_written_terms | D | no | same-model fallback of atlas |
| Anthropic API | anthropic-api | claude-opus-5-5, claude-sonnet-5-5 | C1 | pending_written_terms | D | no | NDA projects run without composer and judge until the written terms are filed |
| Anthropic consumer login | anthropic-consumer-login | - | D | never | D | never | never a route for client data; client material goes only through the API key |
| Runway (web app) | runway | - | D | pending_written_terms | D | no | web app, graded apart from the Dev API route (engine manifests); files made there are imported and count as grade D |
| Higgsfield | higgsfield | - | D | pending_written_terms | D | no | web app and CLI, no adapter; files made there are imported and count as grade D |
| fal | fal | - | D | pending_written_terms | D | no | not in the v1 roster; no adapter is written before its terms are held |

<!-- custody-matrix:end -->

"declared" is the grade in the manifest; "effective" is what the router uses (pending written terms and public uploads are D). "NDA" is whether an NDA project may use the route today.

## 3. Day-1 emails

BUILD_PLAN section 8 lists four day-1 emails: Chaos, Google, OpenAI and Atlas (E4). The Chaos email is about Vantage elements, licences and Max 2027, not custody, so it is not repeated here. Anthropic, Runway, Higgsfield and fal are added because decisions 9 and 10 depend on them. Send each from the account that owns the vendor relationship, ask for the reply in writing from an address the vendor confirms can bind it, and file the reply with its sha256 (promotion rule, section 1). Every question below is a `verify in writing` item from the matrix.

### 3.1 Google (Gemini API and Vertex AI)

To: Google Cloud sales or support for Sthyra's billing account (Vertex), and Gemini API support for the paid Gemini API project. Ask: For Vertex AI, can Sthyra's project run Veo 3.1 and the Nano Banana image models (2.1 and Pro) with zero data retention, what must be configured to get it, and will you confirm in writing that our prompts, images and outputs are neither retained nor used for training? Are those models available in `asia-south1`, and if we call them there does processing and any storage stay in that region? For the paid Gemini API, does Google train on prompts, images or outputs from an Indian developer on the paid tier, how long does it keep them (including any abuse-monitoring logs), and is there any way to pin the region where they are processed or stored? Who are the sub-processors for each service? How do we request deletion of anything already retained?

### 3.2 OpenAI (image API)

To: OpenAI sales or support for Sthyra's API organisation. Ask: Is Sthyra's organisation eligible for zero data retention on the image endpoints (GPT Image 2.5 generate and edit), how do we apply, and will you confirm the approval in writing naming those endpoints? Without ZDR, how long are image inputs and outputs retained and for what purpose? Will you confirm in writing that API inputs and outputs from our organisation are not used for training? Which regions process and store our requests, and is a data-residency option available to us? Who are the sub-processors? How do we request deletion?

### 3.3 Atlas (AtlasCloud)

To: AtlasCloud support or business contact for Sthyra's account. Ask: How long do you keep files uploaded through `/model/uploadMedia`, generation results and request logs, and will you delete them on request? The `download_url` returned for an upload is on an Alibaba OSS host and result URLs are fetched without credentials: are both readable by anyone with the URL, how long do they stay live, and can our account make uploads and results private (signed, expiring or authenticated)? For each model we use (Seedream 5 Pro, Seedance 2.5, Kling O3 4K, Kling v3 Pro, Nano Banana 2.1), which upstream provider and which region serves the request, and do your terms with them forbid training on our inputs? Do you or any upstream provider train on our inputs or outputs? Who are your sub-processors, and do you have a written data-processing agreement we can sign? Also, from section 8: do the displayed 80% and 85% discounts apply to the listed base price?

### 3.4 Anthropic (API under commercial terms)

To: Anthropic sales or support for Sthyra's API organisation. Ask: Under the commercial terms that govern our API key, are prompts, images and outputs used for training? How long are they retained, and is zero data retention available for our organisation? Which regions process and store our requests? Who are the sub-processors? When Claude Code or an MCP client is authenticated with our API key, are the files, images and tool results in that session covered by the same commercial terms? And which terms govern Claude Code when it is signed in with a Claude subscription login instead of an API key?

### 3.5 Runway

To: Runway support or enterprise sales for Sthyra's account. Ask: How long do you keep images and videos uploaded to the web app (and to the API, if we use it), and the outputs? Do you use our uploads or outputs to train or improve models, and can that be switched off in writing for our account? Which regions store and process them, and who are the sub-processors? Which of the models we use are your own and which are provided by a third party? Will you delete everything our account has uploaded so far, and confirm the deletion in writing? Which agreement covers our Dev API project; will you offer Enterprise terms to a studio of our size; confirm the sub-processor for Seedance, Kling and Veo calls.

### 3.6 Higgsfield

To: Higgsfield support for Sthyra's account. Ask: For uploads made through the web app and the `higgsfield` CLI, how long do you keep inputs and outputs? For each model we use, which upstream provider receives our files, and do your terms with them forbid training on our inputs? Do you train on our inputs or outputs? Are uploaded or generated files ever reachable by a public URL, and for how long? Which regions store and process them, and who are the sub-processors? Will you delete everything our account has uploaded so far, and confirm it in writing?

### 3.7 fal

To: fal support or sales. Ask before any fal adapter is written: For each model we would call, does fal run it on its own infrastructure or pass the request to an upstream provider, and which one? How long are uploaded files, outputs and logs kept, and are uploaded files reachable by a public URL? Do fal or upstream providers train on our inputs or outputs? Which regions process and store them, and who are the sub-processors? Is a written data-processing agreement available?

## 4. Retroactive contract checklist (decision 10)

Client plates have already gone to Atlas, Higgsfield and Runway (E2), all of which are grade D. That is why this checklist is overdue, not future work. Until the first five boxes are done for a project, no further plate or derivative from that project leaves the box.

- [ ] Read the TULA agreement(s): confidentiality and NDA terms, ownership of supplied plates and outputs, any clause on sub-contractors, processors or third-party software, and any clause on AI. Record what it permits.
- [ ] Read the Whispers of Trees agreement(s) for the same points.
- [ ] Do the same for each villa contract and any other live client.
- [ ] Build the outbound inventory per project: which files or derivatives went to which vendor, when, through which account and route (web app, CLI, STAFFAGE Atlas client). Use the vendors' job histories, STAFFAGE's takes ledger and the project folders; mark anything that cannot be reconstructed as unknown.
- [ ] Send each vendor in the inventory a written deletion request for that account's past uploads and outputs (questions 3.3, 3.5, 3.6), and record each answer and date.
- [ ] Decide with counsel how to tell each affected client about earlier processing, and offer the consent form with its "earlier processing" section (`docs/consent-template.md`, section 10).
- [ ] Add an AI-processing clause and a sub-processor list (vendor, purpose, grade, region) to the standard contract for new work, drafted with counsel.
- [ ] Keep a signed consent record per project, filed with the project's contract records, and enter its date in the table in section 5.
- [ ] Set the custody class for every live project in section 5 and in its project setting.
- [ ] Note who owns the decision for each item and the date it closed.

## 5. Per-project custody class

| Project | NDA? | Max grade | Named D vendors consented | Video path | Contract read | Consent signed | Outbound inventory done |
|---|---|---|---|---|---|---|---|
| TULA | TO SET | TO SET | TO SET | TO SET | TO SET | TO SET | TO SET |
| Whispers of Trees | TO SET | TO SET | TO SET | TO SET | TO SET | TO SET | TO SET |
| Villa work (one row per villa client) | TO SET | TO SET | TO SET | TO SET | TO SET | TO SET | TO SET |

Until a row is set, no plate or derivative from that project leaves the box (decision 10); in effect it is C0. Even an NDA row set to C1 means local only today, because no C1 route is confirmed. Sthyra-owned scenes and the conformance scene are not client material: they may use any route, still quoted and logged.

## 6. Operational rules

| Rule | Where it lives | Status 2026-10-17 |
|---|---|---|
| The router filters by custody before anything else; pending counts as D; NDA projects are capped at C1, so Atlas and every D route are refused with code `custody`, never a silent fallback (E9). | `engines/plate_engines/router.py` (`effective_grade`, `choose`), `daemon/plate_daemon/custody.py` (`project_allowed`, `require_route`) | In code and tested. |
| Every quote shows provider, model, region and custody grade, and expires in 15 minutes; a human approves (CLAUDE.md doctrine 8). | `daemon/plate_daemon/quotes.py`, the Make bar | In code and tested. |
| An NDA packet's custody floor may list only C0 and C1; the plugin's floor and the project setting both apply and the stricter wins (E18). | `spec/plate_spec`, `custody.project_allowed` | In code. |
| The manifest and ID legend are C0 and never leave the box; `packet_id` is opaque and carries no client or file name (E18). | `spec/PACKET.md` section 12, schema, `egress.py` leak scan | In code: the egress guard blocks a payload carrying a project, packet, camera, layer, object, material or node name. |
| Uploads carry opaque ids and stripped metadata: no EXIF or XMP, and no client, file or layer names (BUILD_PLAN section 7). | `daemon/plate_daemon/egress.py` | In code: PNG only, metadata rewritten out, file names become opaque ULIDs. Fixture-tested; not yet exercised against a live vendor. |
| Every outbound request is logged (BUILD_PLAN section 7). | `<project>/outbound.jsonl` (`outbound_log.py`), fsync'd before the request leaves; the ledger `runs.jsonl` is separate | In code. The Custody panel reads the tail through `GET /api/v0/projects/{id}/outbound?tail=N`. |
| On NDA projects the judge sees only downscaled, cropped, metadata-stripped derivatives, and only once the Anthropic API route is confirmed C1 (E13). | `daemon/plate_daemon/llm/derivative.py`, `custody_routes.yaml` | In code. While the route is pending, NDA projects run with no composer and no judge. |
| The agent channel uses an API key under commercial terms, never the consumer login; MCP tools cannot approve, deliver or export masters; plate content is data, never instructions (E1, E15). | Operator practice, plus `daemon/plate_daemon/mcp_server.py` and `docs/mcp.md` | The tool list is enforced in code; which login a Claude Code session uses is not enforceable: check `/status` before any agent session reads client material. |
| Files made by hand in a vendor web app (Higgsfield, Runway) are imported, gated and ledgered, and count as that vendor's grade for the project (E7). | `docs/runbooks/manual-pipeline-fallback.md` | A procedure, not yet automated: files are scored with the gate commands and recorded in `manual/IMPORT_LOG.md`. |

## 7. Open questions for the custody owner

- Decision 9 has no grade for a first-party API whose written terms forbid training but keep data for a window. Such a route does not meet C1 as defined and is not a reseller, so it stays D until Fable and Aasish decide.
- A client may consent to one named D vendor for one project. The router takes a project max grade, which cannot express "this D vendor only". Until a per-project vendor allowlist exists, check every quote against the signed consent before approving it.
- Grades for the offsite backup and the review-bundle host (section 2.3).

## Change log

| Date | Change | By |
|---|---|---|
| 2026-10-17 | First version. All cloud routes D today; OpenAI, Gemini, Vertex and Anthropic API recorded as C1 candidates pending written terms; Atlas D on observed public uploads with C2 achievable. | Opus 5.5 (task T9), for Fable and Aasish review |
| 2026-10-17 | Section 6 brought up to date with the built code (router, quotes, egress, outbound log, judge derivatives, MCP); section 2.4 added: the matrix generated from `custody.matrix_markdown()`. No grade changed. | Sonnet 5.5 (task T51) |
| 2026-10-18 | Runway Dev API route added as D on the evidence (s4.4), private uploads, C2 path; web-app row kept apart. | Sonnet 5.5 (task T124), Wave 11 |

# Client consent: third-party AI processing (template)

**This is a template for Sthyra to adapt with counsel. It is not legal advice and has not been reviewed by a lawyer.** Everything in [BRACKETS] is a placeholder to fill or delete. Items marked `counsel to review` need a lawyer's reading before the form is used. Fill the sub-processor list from `docs/custody.md` on the day of signing, and never tick a vendor whose grade or region is not filled in from that file. Keep the wording plain: if Sthyra could not explain a sentence to the client across a table, rewrite it.

---

## 1. Parties

- **Client:** [CLIENT LEGAL NAME], [REGISTERED ADDRESS], represented by [NAME, TITLE].
- **Studio:** [STHYRA CONTRACTING ENTITY LEGAL NAME], [REGISTERED ADDRESS], represented by [NAME, TITLE].
- **Main agreement:** [AGREEMENT TITLE, REFERENCE AND DATE]. This consent adds to it. Where the two conflict, the one that protects the client's material more strictly applies. `counsel to review`
- **Confidentiality:** this project [IS / IS NOT] under a non-disclosure agreement ([NDA REFERENCE AND DATE]).

## 2. Project

- **Project name:** [PROJECT NAME]
- **Deliverables covered:** [STILLS / FILM SECTIONS / CUTDOWNS / OTHER]
- **Period:** from [START DATE] until [END DATE OR "delivery plus N months"].

## 3. What material this covers

- **Plates:** renders Sthyra makes for the project and images the client supplies.
- **Shot packets:** the project files Sthyra exports from 3ds Max (camera, depth, ID passes, the approved 16-bit master). These stay on Sthyra's machines. They are never sent to an AI vendor.
- **Derivatives:** copies made from a plate for one job, such as an 8-bit PNG of the frame, a crop of one region, or a downscaled copy. Only derivatives are ever sent to a vendor.
- **Text:** prompts and instructions Sthyra writes for each job.
- **Outputs:** what a vendor returns, and the finished frames Sthyra composites from them.

## 4. What is never sent to any vendor

- The approved 16-bit master and the EXR passes. Vendors receive only 8-bit derivatives. [OPTIONAL, agree with the client for a confidential project: no derivative is sent larger than [N] pixels on its long side.] `counsel to review`: whether an 8-bit full-frame derivative counts as "the master" under the NDA in section 1.
- The shot packet's manifest and ID legend, which contain client, file and layer names.
- File metadata: EXIF, XMP, original file names, client names and project names. Uploads go under opaque ids that carry none of these.
- Scene files (.max, .vrscene), drawings and any document the client marks confidential.
- On confidential projects, the AI quality judge sees only downscaled, cropped copies with metadata removed.

## 5. Sub-processors

Fill from `docs/custody.md` as of [DATE OF THAT FILE OR COMMIT ID]. Grades: **C0** local only; **C1** the model maker's own API, with written no-training and zero-retention terms held by Sthyra; **C2** a named reseller whose terms Sthyra holds; **D** terms unknown or not held, or uploads become public. A vendor waiting for written terms counts as D.

| Vendor | What it does on this project | Grade today | Region | Written terms held (date) | Allowed? |
|---|---|---|---|---|---|
| Sthyra machines (render box, studio machines) | compositing, measurement, local AI models | C0 | [CITY, COUNTRY] | n/a | always |
| [VENDOR] | [PURPOSE] | [C1 / C2 / D] | [REGION, OR "not stated by vendor"] | [DATE OR "none"] | [ ] yes [ ] no |
| [VENDOR] | [PURPOSE] | [C1 / C2 / D] | [REGION, OR "not stated by vendor"] | [DATE OR "none"] | [ ] yes [ ] no |
| [OFFSITE BACKUP PROVIDER] | encrypted nightly backup; the provider holds only encrypted copies | [GRADE] | [REGION] | [DATE OR "none"] | [ ] yes [ ] no |
| [REVIEW HOSTING PROVIDER] | review copies (watermarked proxies, not masters) behind expiring links | [GRADE] | [REGION] | [DATE OR "none"] | [ ] yes [ ] no |

Sthyra will tell the client in writing at least [N] days before adding a vendor to this list. A new vendor is used only after the client ticks it here (or in a signed addendum). `counsel to review`

## 6. The client's choice

Tick one.

- [ ] **A. Local only.** Nothing leaves Sthyra's machines. AI work uses only models running on Sthyra's own hardware. Some services may be slower or unavailable; motion is rendered in V-Ray or Vantage, or made as local previs.
- [ ] **B. Local plus C1 vendors.** Sthyra may also use the vendors in section 5 that are graded C1 on the date of signing and ticked "yes". Today there is no C1 vendor for video, so video stays as in option A unless option C is also ticked for a named video vendor.
- [ ] **C. Named vendors for this project.** In addition to option A or B, Sthyra may use only the vendors below, each of which the client has read about in section 5. The client understands Sthyra does not hold written retention or training terms for a D vendor.

| Vendor | Grade | Used for | Client initials |
|---|---|---|---|
| [VENDOR] | [C2 / D] | [E.G. "video clips from approved stills"] | [  ] |
| [VENDOR] | [C2 / D] | [PURPOSE] | [  ] |

**Internal quality calibration** (separate from the choice above): Sthyra keeps its own ratings of AI outputs from this project, and may keep those outputs on its own machines, to tune its quality checks. Nothing is sent to a vendor for this. [ ] allowed [ ] not allowed

## 7. Purposes

Material is processed only to produce the project's deliverables:

- adding or adjusting people, vegetation, sky and lighting variants inside areas Sthyra has marked as editable, while the architecture stays as rendered;
- re-photographing close and medium frames;
- making motion clips from approved stills;
- checking quality, including an AI judge that reports problems (it can never pass a frame that fails a measured check);
- writing prompts for the above.

Material is not used to make anything for another client. Apart from the internal quality calibration the client may allow in section 6, Sthyra does not use it to train or tune any model.

## 8. Retention and deletion

- **On Sthyra's machines:** project material is kept until [N] months after final delivery, then deleted from Sthyra's machines and backups, unless the client asks in writing for it to be kept longer.
- **At vendors:** Sthyra asks each vendor to delete uploads and outputs where the vendor offers deletion, and records the answer per job. Sthyra can promise only what each vendor's written terms say; for a D vendor that is unknown, which is why D vendors need the client's tick in section 6.
- **On request:** the client may ask Sthyra to delete the project's material at any time. Within [N] business days Sthyra deletes it from its machines and backups, sends the vendor deletion requests, and gives the client a written list of what was deleted and each vendor's answer. Delivered deliverables stay with the client.
- **Records:** Sthyra keeps a log of every upload to a vendor (vendor, date, which kind of derivative, under which opaque id) and will give the client a copy on request. [Only promise this once Sthyra's outbound log is running.] `counsel to review`

## 9. Withdrawing consent

The client may withdraw this consent, in full or for one vendor, by writing to [STHYRA CONTACT NAME AND EMAIL]. From receipt:

- nothing new from the project is sent to the withdrawn vendors;
- jobs not yet sent are cancelled; a job already sent cannot be recalled, but its output is not used without the client's say;
- Sthyra sends the deletion requests in section 8 and reports back within [N] business days;
- the work continues under option A (or the remaining ticked vendors), and Sthyra tells the client of any effect on dates or fees. [FEE AND SCHEDULE TERMS] `counsel to review`

## 10. Earlier processing (use only if material from this project was already sent)

Before this consent, Sthyra sent the following project material to these vendors:

| Vendor | What was sent | When | Deletion requested (date) | Vendor's answer |
|---|---|---|---|---|
| [VENDOR] | [DESCRIPTION] | [DATES] | [DATE] | [ANSWER OR "awaiting"] |

The client chooses: [ ] accept the earlier processing as described [ ] require deletion requests to all listed vendors and a written report. `counsel to review`

## 11. Personal data (India)

Plates and outputs can include identifiable people, for example in a client-supplied photograph. Where they do, the Digital Personal Data Protection Act, 2023 may apply to how the material is handled and sent abroad. `counsel to review`: the parties' roles under that Act, any notice owed to the people shown, and the transfer of that material to vendors outside India.

## 12. Signatures

**Client**

Name: [NAME]  
Title: [TITLE]  
Signature: ______________________  
Date: [DATE]

**Sthyra**

Name: [NAME]  
Title: [TITLE]  
Signature: ______________________  
Date: [DATE]

## 13. Change log

| Date | Version | Change | Client initials | Sthyra initials |
|---|---|---|---|---|
| [DATE] | 1 | Signed. | [  ] | [  ] |
| [DATE] | [N] | [E.G. "Vendor added to section 5 and ticked"] | [  ] | [  ] |

---

Template history (delete this line from the client copy): 2026-10-17, first draft by Opus 5.5 (PLATE task T9) from BUILD_PLAN decisions 9, 10, 21, 22, 23 and `docs/custody.md`, for Sthyra and counsel to adapt.

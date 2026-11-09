# Security Policy

This document defines how security vulnerabilities are reported, triaged, remediated, and disclosed for software developed and maintained by **[ORGANIZATION / OWNER NAME]** (“we,” “us,” or “the project”).

It is intended for use across repositories and internal software programs. Replace bracketed placeholders before publishing. Keep this file at the repository root as `SECURITY.md` so GitHub surfaces it under the Security tab.

---

## 1. Principles

1. **Protect users first.** Prefer coordinated, private disclosure over public discussion until a fix or mitigation is available.
2. **Assume good faith.** Researchers and employees who follow this policy will receive safe harbor (see §8).
3. **Severity drives urgency.** Response and remediation timelines scale with impact (see §5).
4. **Least disclosure necessary.** Share technical details only with people who need them to fix or validate the issue.
5. **No security through obscurity as a primary control.** Fixes must address root cause, not merely hide the issue.

---

## 2. Scope

### 2.1 In scope

Unless a repository README or advisory says otherwise, the following are in scope when they affect this project or its supported deployments:

| Area | Examples |
|------|----------|
| Application security | Injection, XSS, CSRF, SSRF, authn/authz bypass, privilege escalation, insecure deserialization |
| Cryptography & secrets | Weak crypto, hardcoded credentials, secret leakage, insecure key handling |
| Data protection | Unauthorized access to PII, credentials, tokens, or other sensitive data |
| Supply chain | Compromised dependencies, malicious packages, tampered build artifacts attributable to this project |
| Infrastructure as code / config | Insecure defaults shipped by this project that create exploitable conditions |
| CI/CD & release integrity | Poisoned pipelines, unsigned or substitutable release artifacts produced by this project |

### 2.2 Out of scope

The following are generally **out of scope** unless they demonstrate a novel, high-impact vulnerability in this project:

- Social engineering, phishing, or physical attacks against individuals or offices
- Denial-of-service against shared cloud providers, CDNs, or third-party SaaS you do not own
- Reports based solely on missing security headers or best-practice recommendations without a demonstrated exploit path
- Vulnerabilities in third-party products or dependencies **with no project-specific aggravating factor** (report those upstream; tell us if we should bump or pin)
- Issues requiring rooted/jailbroken devices, broken client trust stores, or MITM where the user already fully controls the client
- Spam, rate-limit exhaustion, or “clickjacking” without a practical impact scenario
- Theoretical issues without a reasonable reproduction path

If you are unsure whether something is in scope, **report it privately**. We would rather triage an out-of-scope report than miss a real issue.

### 2.3 Supported versions

Only the versions below receive security fixes. Older versions may be unsupported even if still runnable.

| Version / Branch | Supported |
|------------------|-----------|
| `[e.g. main / latest release]` | :white_check_mark: |
| `[e.g. previous minor, N-1]` | :white_check_mark: / :x: |
| Older releases | :x: |

Update this table per repository. If a product has an LTS channel, list it explicitly.

---

## 3. How to report a vulnerability

### 3.1 Preferred channels (use one)

**Do not** open a public GitHub Issue, Discussion, Pull Request, or social-media post to report a security vulnerability.

Use one of the following, in preferred order:

1. **GitHub Private Vulnerability Reporting**  
   Repository → **Security** → **Advisories** → **Report a vulnerability**  
   (Enable “Private vulnerability reporting” in repository settings if it is not already on.)

2. **Security email**  
   `[security@example.com]`  
   Subject line: `[SECURITY] <short title> — <repo or product name>`

3. **Internal ticket / SOAR** (employees and contractors only)  
   `[e.g. ServiceNow / Jira Security queue URL or project key]`  
   Mark the ticket **confidential / restricted** per your data-classification standard.

### 3.2 Encryption (recommended for email)

If available, encrypt email reports with our PGP key:

- Key ID / fingerprint: `[FINGERPRINT]`
- Public key: `[URL to .asc or keyserver]`

If encryption is unavailable, still report promptly; do not delay disclosure solely for encryption setup. Avoid including live production secrets in email when a redacted reproduction is sufficient.

### 3.3 What to include

Provide as much of the following as you can. Incomplete reports are still accepted; more detail speeds triage.

| Field | Guidance |
|-------|----------|
| **Title** | One-line summary |
| **Type** | e.g. RCE, SQLi, auth bypass, path traversal, secret exposure |
| **Affected asset** | Repo, package, service, commit SHA, tag, or release version |
| **Environment** | OS, runtime, deployment mode, feature flags, relevant config |
| **Steps to reproduce** | Numbered, minimal, deterministic steps |
| **Proof of concept** | Script, request/response, or screenshots; keep PoCs non-destructive |
| **Impact** | What an attacker can achieve (read data, escalate, lateral movement, etc.) |
| **Affected users / data** | Classes of data or roles impacted, if known |
| **Suggested severity** | Your estimate using §5 (optional) |
| **Suggested fix** | Patch idea or mitigation (optional) |
| **Disclosure plans** | Whether you have shared this elsewhere, and any planned public disclosure date |
| **Contact** | Preferred handle/email and timezone |

### 3.4 Proof-of-concept rules

- Prefer **minimal, non-destructive** PoCs.
- Do **not** access, modify, or exfiltrate data beyond what is necessary to demonstrate impact.
- Do **not** pivot to other systems, spam users, or degrade production availability.
- If you encounter live credentials or personal data, **stop**, redact them in your report, and tell us immediately.

---

## 4. Triage and communication

### 4.1 Acknowledgement

We aim to acknowledge receipt within **[2 business days]** (target: **1 business day** for Critical reports).

If you do not receive an acknowledgement within that window, follow up once on the same channel. Do not create a public issue as a follow-up.

### 4.2 Status updates

After acknowledgement, we will:

1. Assign a tracking ID / advisory draft.
2. Confirm whether the report is in scope and reproducible.
3. Provide a severity rating (may differ from your estimate).
4. Share a remediation plan and target fix window when known.
5. Coordinate disclosure timing with you.

Expected update cadence while a confirmed issue is open: **at least weekly**, or sooner on status changes (validated, fixed, released, disclosed).

### 4.3 Outcomes

| Outcome | Meaning |
|---------|---------|
| **Accepted** | Confirmed vulnerability; remediation planned |
| **Duplicate** | Already known / already tracked |
| **Informative** | Useful hardening advice; not treated as a vulnerability |
| **Out of scope** | Does not meet §2 criteria |
| **Not reproducible** | Cannot validate with provided information (we may ask for more detail) |
| **Spam / abusive** | Ignored; may result in loss of safe harbor |

---

## 5. Severity and remediation targets

We classify issues using a CVSS v3.1 / v4.0-compatible approach, adjusted for real-world exploitability and business impact.

| Severity | Typical CVSS | Examples | Remediation target (after confirmation) |
|----------|--------------|----------|-----------------------------------------|
| **Critical** | 9.0–10.0 | Unauth RCE, widespread credential compromise, trivial auth bypass on sensitive systems | Fix or mitigating control within **[7 days]**; emergency release as needed |
| **High** | 7.0–8.9 | Auth’d RCE, significant data exposure, privilege escalation | Within **[30 days]** |
| **Medium** | 4.0–6.9 | Limited data exposure, CSRF with impact, insecure defaults with constraints | Within **[90 days]** |
| **Low** | 0.1–3.9 | Minor info leaks, defense-in-depth gaps with hard prerequisites | Best effort / next scheduled release |

These are **targets**, not contractual SLAs, unless a separate agreement says otherwise. Complex fixes, dependency upstream delays, or change-control constraints may extend timelines; we will communicate that clearly.

**Mitigations** (config changes, feature flags, WAF rules, access restrictions) may be deployed ahead of a permanent code fix when appropriate.

---

## 6. Coordinated disclosure

### 6.1 Embargo

Please keep vulnerability details confidential until:

1. We have released a fixed version (or an effective mitigation), **and**
2. We have agreed a disclosure date with you, **or**
3. The embargo period below expires without an agreed extension.

**Default embargo:** **[90 days]** from our acknowledgement of a valid, in-scope report, unless we agree otherwise in writing.

We may request a short extension for especially complex issues or coordinated multi-vendor fixes. Unreasonable silence or refusal to engage may lead researchers to disclose after the embargo; we ask that you give **[7 days]** notice before public disclosure in that case.

### 6.2 Public disclosure process

When ready, we typically:

1. Publish a GitHub Security Advisory (and/or internal bulletin).
2. Credit the reporter (unless anonymity is requested).
3. Reference fixed versions / commits and any required operator actions.
4. Assign or request a CVE when appropriate (`[CVE CNAs / process notes]`).

We do **not** publish exploit code in advisories unless needed for operators to validate patches, and even then we keep it minimal.

### 6.3 Reporter credit

With your consent, we will acknowledge your contribution in the advisory and, where applicable, a security hall-of-fame / release notes. You may request a pseudonym or anonymity.

---

## 7. Bug bounty and compensation

`[Choose one and delete the others]`

- **No bounty:** This project does not operate a paid bug bounty. We appreciate responsible reports and will provide credit.
- **Internal recognition only:** Eligible employee/contractor reports may receive `[recognition / performance credit]` per internal policy; no cash bounty.
- **Paid program:** Eligible reports may receive rewards under `[program name / URL]`. Bounty eligibility, amounts, and stacking rules are defined by that program, not this file.

Duplicate, out-of-scope, or previously known issues are not eligible for reward.

---

## 8. Safe harbor

If you make a good-faith effort to comply with this policy during security research, we will:

1. Not pursue civil or criminal claims against you for the research described in a timely report.
2. Not file a complaint or refer the matter for law enforcement if your research stays within §2 and §3.4.
3. Consider your research authorized under applicable anti-circumvention / computer-fraud statutes to the extent we can authorize it for systems we own or operate.

Safe harbor **does not** apply if you:

- Extort, threaten, or demand payment outside an official bounty process
- Exfiltrate or retain sensitive data beyond demonstration needs
- Intentionally disrupt availability or destroy data
- Pivot into unrelated systems or third-party tenants
- Publicly disclose before coordinated disclosure terms are met (except after a properly noticed embargo expiry)

If at any point you are unsure whether an action is authorized, **stop and ask** via a reporting channel in §3.1.

---

## 9. Handling secrets and sensitive data found in the wild

If you discover credentials, API keys, private keys, session tokens, or personal data in this repository, logs, packages, or related systems:

1. Report via §3.1 immediately.
2. Do not use the secret to access further systems except as minimally needed to prove validity (prefer metadata-only checks).
3. Do not share the secret in public issues, chat, or screenshots.
4. We will rotate/revoke affected credentials and assess blast radius.

Contributors must never commit secrets. Use environment variables, a secret manager, or CI secret stores. If you accidentally commit a secret, treat it as compromised: rotate it, then contact us—history rewrites alone are not sufficient.

---

## 10. Secure development expectations (contributors)

Anyone contributing code, infrastructure, or dependencies to this project should:

- Follow secure coding practices appropriate to the language and framework.
- Avoid introducing known-vulnerable dependencies; justify exceptions.
- Not weaken authn/authz, crypto, or input validation without explicit review.
- Run available linters, SAST, secret scanning, and tests before opening a PR.
- Flag security-sensitive changes in the PR description (auth, crypto, parsers, deserialization, file I/O, network exposure, CI permissions).
- Prefer merges that keep the default configuration safe.

Security-relevant changes may require additional review from `[security champion / CODEOWNERS path]`.

---

## 11. Dependency and supply-chain vulnerabilities

- Report project-specific exploitability even if the root cause is upstream.
- For pure upstream CVEs with no project aggravation, prefer upstream reporting and open a normal (non-security) dependency-update PR if a fixed version exists.
- We monitor GitHub Dependabot / advisory alerts where enabled and prioritize by exploitability and reachability, not CVE score alone.

---

## 12. Incident response (operators / maintainers)

For confirmed incidents involving production systems that run this software:

1. Contain and preserve evidence per `[incident response runbook URL]`.
2. Engage `[Security / On-call rotation]` via `[pager / Slack / phone bridge]`.
3. Assess confidentiality, integrity, and availability impact; classify data involved.
4. Remediate, rotate credentials, and notify affected parties per legal/compliance requirements.
5. Complete a post-incident review; file durable fixes in this repository when applicable.

This section guides internal responders; external reporters should still use §3.

---

## 13. Compliance and standards alignment

This policy is designed to be compatible with common enterprise expectations, including coordinated vulnerability disclosure practices and secure SDLC controls. Mapping to specific frameworks (e.g., NIST SSDF, ISO/IEC 27001, SOC 2, PCI DSS) is maintained outside this file in `[GRC / control library link]`.

Do not interpret this document as a certification, attestation, or contractual security commitment unless incorporated by a separate signed agreement.

---

## 14. Policy maintenance

| Item | Detail |
|------|--------|
| **Owner** | `[Security team / maintainer name]` |
| **Review cadence** | At least annually, and after material process changes |
| **Effective date** | `[YYYY-MM-DD]` |
| **Last reviewed** | `[YYYY-MM-DD]` |

Changes to this policy take effect when merged to the default branch of this repository.

---

## 15. Contact summary

| Purpose | Contact |
|---------|---------|
| Vulnerability reports | GitHub Private Vulnerability Reporting or `[security@example.com]` |
| Security questions (non-vuln) | `[security@example.com]` |
| Urgent production incident (internal) | `[on-call / IR contact]` |
| PGP | `[fingerprint / URL]` |

Thank you for helping keep this software and its users safe.

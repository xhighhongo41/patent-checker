# INPADOC legal-status codes — frequent codes reference (v0.2)

Codes and descriptions below were collected verbatim from real INPADOC
legal responses during the v0.1 manual runs across six jurisdictions. This
table anchors the meaning of the codes you will see most often; it is not
exhaustive. Legal-event interpretation stays your job: read the full event
list chronologically and let the latest decisive event determine the
status. When in doubt, quote the event list in the report instead of
guessing.

General rules:

- The authority for legal status is INPADOC (`patent-checker legal`).
  Google Patents status labels are reference values only (v0.1 measured
  disagreement in 5 of 10 sampled documents).
- Judge per jurisdiction: the same concept is coded differently per office.
- An event's presence proves procedure, not current force: e.g. a grant
  event followed by a lapse event means the right is no longer in force.
- Family members are judged individually — never infer one country's
  status from another's (expand the family first).

## JP

| Code | INPADOC description (verbatim) | Reading |
|---|---|---|
| A621 | WRITTEN REQUEST FOR APPLICATION EXAMINATION | Examination requested (application in prosecution) |
| A131 | NOTIFICATION OF REASONS FOR REFUSAL | Office action (rejection reasons) issued |
| A521 | REQUEST FOR WRITTEN AMENDMENT FILED | Amendment filed |
| A01 | WRITTEN DECISION TO GRANT A PATENT OR TO GRANT A REGISTRATION (UTILITY MODEL) | Decision to grant |
| A61 | FIRST PAYMENT OF ANNUAL FEES (DURING GRANT PROCEDURE) | Grant fees paid (registration imminent/done) |
| R150 | CERTIFICATE OF PATENT OR REGISTRATION OF UTILITY MODEL | Patent registered |
| A761 | WRITTEN WITHDRAWAL OF APPLICATION | Application withdrawn (dead) |

## CN

| Code | INPADOC description (verbatim) | Reading |
|---|---|---|
| PB01 | PUBLICATION | Application published |
| SE01 | ENTRY INTO FORCE OF REQUEST FOR SUBSTANTIVE EXAMINATION | Substantive examination started |
| GR01 | PATENT GRANT | Granted |
| CF01 | TERMINATION OF PATENT RIGHT DUE TO NON-PAYMENT OF ANNUAL FEE | Right terminated (dead) — check the event date |
| C02 | DEEMED WITHDRAWAL OF PATENT APPLICATION AFTER PUBLICATION (PATENT LAW 2001) | Application deemed withdrawn (dead) |

## US

| Code | INPADOC description (verbatim) | Reading |
|---|---|---|
| STPP | INFORMATION ON STATUS: PATENT APPLICATION AND GRANTING PROCEDURE IN GENERAL | Status information (read the event text) |
| STCF | INFORMATION ON STATUS: PATENT GRANT | Granted |
| FEPP | FEE PAYMENT PROCEDURE | Fee event (read the event text) |
| MAFP | MAINTENANCE FEE PAYMENT | Maintenance fee paid (right maintained) |
| AS | ASSIGNMENT | Ownership assignment (not a status change) |

## KR

| Code | INPADOC description (verbatim) | Reading |
|---|---|---|
| A201 | REQUEST FOR EXAMINATION | Examination requested |
| WITB | WRITTEN WITHDRAWAL OF APPLICATION | Application withdrawn (dead) |
| F11 | IP RIGHT GRANTED FOLLOWING SUBSTANTIVE EXAMINATION | Granted |
| U11 | FULL RENEWAL OR MAINTENANCE FEE PAID | Maintenance fee paid (right maintained) |

## EP

| Code | INPADOC description (verbatim) | Reading |
|---|---|---|
| PG25 | LAPSED IN A CONTRACTING STATE [ANNOUNCED VIA POSTGRANT INFORMATION FROM NATIONAL OFFICE TO EPO] | Lapsed in one contracting state only — the EP right may remain in force elsewhere; list the states |

## GB

| Code | INPADOC description (verbatim) | Reading |
|---|---|---|
| PCNP | PATENT CEASED THROUGH NON-PAYMENT OF RENEWAL FEE | Ceased (dead) — check the event date |

## Known response quirks (v0.1 measured)

- A GB "A" publication can return a legal response with no events at all;
  the tool retries the "B" publication automatically. If both are empty,
  treat the status as unknown and say so in the report.
- The legal response's `total-result-count` reflects the family size while
  the response body lists only the queried publication; fetch each family
  member you need explicitly.

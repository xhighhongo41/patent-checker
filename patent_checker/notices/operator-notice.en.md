# Patent Checker — Operator Notice (version 1.0)

This notice is addressed to whoever runs the Patent Checker MCP server
("the operator"). The server refuses to start until the operator has
acknowledged this notice by setting the environment variable
`PATENT_CHECKER_OPERATOR_CONSENT` to the version number shown above. If the
notice changes, its version number changes and the server asks for a fresh
acknowledgement.

## 1. Data-source terms and rate limits are your responsibility

The server retrieves data from the EPO Open Patent Services (OPS) and from
Google Patents. Use of those services is governed by their own terms: for
OPS, the terms attached to your registered application, including its
fair-use quotas; for Google Patents, Google's terms of service. The server
spaces its requests and follows the throttling signals OPS sends back, but it
cannot know your quota or how many other clients share your credentials.
Staying within those terms is the operator's responsibility.

## 2. Credentials are yours to protect

OPS credentials and the server's bearer token are read from environment
variables (or from files named by the corresponding `_FILE` variables). They
are never written to the server's logs, raw-data store, or responses. Anyone
who can reach the server and presents the bearer token can spend your OPS
quota. Keep the token secret, bind the server to the loopback interface
unless you deliberately share it, and put TLS in front of it if you do.

## 3. What the server receives and stores

The server receives only search expressions (CQL) and publication numbers,
and returns patent data derived from public sources. It does not receive,
and therefore cannot store, the source code, documents, or project
descriptions of the people who use it. It keeps raw API responses, a request
log (timestamps, request kinds, URLs, status codes and throttling headers),
and a cache of retrieved patent data in its data directory. Search
expressions appear in that log and cache; treat the data directory as
sensitive to that extent.

## 4. The consent of the people who use the server

The Skill that drives this server shows each of its users a separate legal
notice (covering the non-judgmental nature of results, willful infringement,
the incompleteness of searches, and professional advice) and records their
consent on their own machines. This server does not check that consent. If
you provide the server to a team, telling them about that notice and about
items 1-3 above is your responsibility.

## 5. No warranty; no legal advice

The server, the Skill, and every result they produce are provided as is,
without warranty of any kind. Nothing they produce is a legal opinion or a
determination of infringement or non-infringement. The authors accept no
responsibility for how the server is operated or for decisions made on the
basis of its output.

By setting `PATENT_CHECKER_OPERATOR_CONSENT=1.0`, you confirm that you have
read and understood items 1-5 above.

# Preparing and publishing a release

Use this procedure for the initial public beta and subsequent releases. The
release notes live in [RELEASE_NOTES.md](../RELEASE_NOTES.md); evaluation evidence
is indexed in [model evaluation](model-evaluation.md) and
[RAG evaluation](rag-lite-report.md).

## Validate the release tree

With Python 3.12 and the dependencies from `requirements.txt` installed:

```bash
.venv/bin/python tests/mock_e2e.py --all
.venv/bin/python tests/proxy_e2e.py
.venv/bin/python benchmarks/v2/tests/run_v2_tests.py
.venv/bin/python benchmarks/v2/harness/lint.py
.venv/bin/python benchmarks/v2/harness/parity.py
git diff --check
```

The mock suites use temporary data and fake services. They do not establish
compatibility with every live mail provider or model. The private-corpus retrieval
evaluator is not a fresh-clone gate.

## Check a fresh container

Build a separately named image and start it with empty state. These commands
do not use the live Compose service or its `data/` mount:

```bash
docker build -t mail-triage:release-check .
docker run -d --name mail-triage-release-check \
  -p 127.0.0.1:18097:8097 \
  --add-host host.docker.internal:host-gateway \
  -e LLM_BASE_URL= -e LLM_MODEL= \
  mail-triage:release-check
curl --fail http://127.0.0.1:18097/healthz
docker exec mail-triage-release-check python app.py --doctor
```

Wait for `/healthz` to return 200. Verify the setup wizard in a browser and check
that missing mailbox/model configuration is explained rather than treated as a
successful connection. Confirm the host alias resolves if documenting a host-side
model endpoint. This smoke test does not download/index real mail or validate a
live model.

When finished, remove only the disposable container:

```bash
docker rm -f mail-triage-release-check
```

For a normal install, follow [Getting started](getting-started.md), using a fresh
`.env` copied from `.env.example`. Never copy live state into a release artifact.

## Publish

After the reviewed release changes are committed, merged into `master`, pushed,
and the release commit has passed the checks:

1. Update the prepared release heading with the chosen version/date and record
   the validation results and commit.
2. Create an annotated tag on that commit. A suggested initial version is
   `v0.1.0-beta.1`; the version remains a choice until publication.
3. Push the tag normally and create a GitHub release using the matching notes.
4. Link the release to installation instructions and committed evidence. Do not
   attach `.env`, databases, token caches, downloaded model weights, or raw mailbox
   exports.

Publishing a release and deploying the author's live mailbox are separate
operations. Deploy only from the clean, merged main checkout using the repo's
documented deployment commands.

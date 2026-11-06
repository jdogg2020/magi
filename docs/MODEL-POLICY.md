# Magi and Nomadobile model policy

Magi is the open-source observation platform developed under Nomadobile LLC.
The platform remains Apache-2.0. Nomadobile's future proprietary classifiers
are separately developed and licensed components, not a relicensing of Magi.

## Local inference is the current goal

The current model target is the Raspberry Pi 5 with the Hailo AI HAT. Training
and compilation may run on a separate development machine, but inference runs
on the station. Do not introduce a hosted inference dependency, upload footage,
require a cloud account, or add a subscription/activation check as part of this
policy. Missing or failed model inference must not interrupt recording.
Existing off/shadow modes and advisory-only acceptance gates remain in force.
This document does not claim that a trained model or commercial package exists.

## Public and private boundary

Keep capture, recording, review, export formats, sensor integrations, basic
detection, generic inference adapters, and model-manifest interfaces public.
Operators must be able to run the platform without Nomadobile's model and use
compatible alternative models subject to those models' own licenses.

Future proprietary training pipelines, curated labels and datasets, evaluation
holdouts, training recipes, checkpoints, weights, and compiled Hailo HEFs belong
in separate private repositories/storage, outside the Magi checkout. Keep access
limited to authorized collaborators. Do not attach these assets to public issues,
PRs, CI artifacts, logs, or releases. A public branch or PR is already public;
CI cannot prevent disclosure after a push.

The existing public `scripts/hailo-training-prep/` tools, manifests, examples,
and `src/magi/hailo.py` scaffolding remain under their existing applicable terms.
Do not delete history or claim that previously published material is secret.
New company-specific training work must start in the private workspace.
Third-party material retains its own terms; this policy does not claim ownership
of external datasets or contributions.

## Model distribution

Distribute the Pi model separately from the Apache source release, with explicit
model license terms reviewed before distribution. Preserve the existing public
manifest contract; model/version, checksum, preprocessing, compatible runtime
and compiler details must be available for reliable operation and troubleshooting.
Keep private training records separate from the distributable manifest.

A compiled HEF is delivered to an operator-controlled device. Compilation,
obfuscation, and encryption are not guarantees against copying or extraction.
Confidentiality of the training process and dataset, appropriate contracts, and
ongoing model quality are the protection strategy. Do not promise unextractable
weights or treat a checksum as copy protection.

## Data and ownership before training or distribution

- Record the source, version, license, commercial-training permissions and
  redistribution constraints of every dataset, base model and toolchain.
- Keep NightSkyUCP excluded from the proprietary/commercial pipeline until its
  conflicting license terms are resolved; see the training-preparation README.
- Obtain explicit permission covering training uses for contributed footage and
  labels. Exporting an event is not consent to upload it or transfer its rights.
- Document appropriate ownership/assignments to Nomadobile LLC and contributor
  or contractor terms. This policy itself transfers no IP and is not a model
  license, an NDA, or an executed assignment.

## Accidental-publication safeguards

`.gitignore` covers common model formats and reserved private-data directories.
`python3 scripts/check_public_assets.py` checks tracked filenames before a push.
The source release builder also rejects these paths, including untracked files
within its packaged directories, before creating an archive.

These are filename-based accident checks, not a content/secret scanner or an
access-control system. Renamed files, arbitrary JSON labels, and private code
cannot be reliably identified this way. Keep private work outside this checkout
and review staged changes and release contents. Any intentional public model
fixture needs an explicit policy/code review, not a force-add workaround.

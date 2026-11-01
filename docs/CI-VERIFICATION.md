# Verification reuse

Every PR runs the fast Python tests, service/permission checks, two-camera
synthetic dashboard tests, Chromium checks and release packaging. The expensive
`test_star_solve` and `test_auto_calibration` modules can reuse successful checks
for identical inputs. All other tests, including calibration safety and optional
hint/thermal checks, still run.

The planner fingerprints each group's inputs. Solver inputs include its test,
local Python import dependencies (including imports inside functions), catalogs,
requirements, package metadata, service units, workflow/dependency setup and CI
policy/tests. Calibration integration conservatively includes every runtime
Python module and Python installer/helper script because it exercises the
application, CLI and persistence. New local imports extend the solver input set.
Dynamic imports or errors disable reuse. A release-number-only change does not
invalidate astronomy evidence; other package metadata remains covered.

UI JavaScript/HTML/CSS and documentation changes do not invalidate astronomy
proof. Application changes can reuse the pure solver group while rerunning the
calibration integration group. Storage is a solver dependency, so storage code
changes still require its expensive tests. This is intentionally conservative.

Evidence is an artifact from a completed successful run of this workflow in this
repository, with the expected group fingerprint and matching origin head. Fork,
failed, incomplete, mismatched and expired evidence is rejected. Only metadata
is read: artifact contents are never executed. Evidence expires after 30 days;
reusing evidence does not refresh its lifetime. Missing evidence or API errors
run the tests. The first run after changing CI policy runs both groups fully.

Successful PR verification still records the **exact complete source tree**.
An ordinary merge of that tree can reuse the complete PR result on main. A
changed tree or absent exact-tree evidence triggers verification again, using
the same conservative group rules. Main does not overwrite existing releases.

Use **Actions → Verify and package → Run workflow → force_full** to run every
test explicitly. That manual run verifies but does not publish a release.

## Examples with current, successful evidence

| Change | Solver tests | Calibration integration | Other checks |
| --- | --- | --- | --- |
| UI text, layout, or documentation | Reuse | Reuse | Run |
| Application/controller Python | Reuse if solver dependencies unchanged | Run | Run |
| Star solver, sensor geometry, or storage dependency | Run | Run | Run |
| Dependency locks, CI policy/workflow, or service units | Run | Run | Run |
| Version number only | Reuse | Reuse | Run |
| Missing/expired proof or force_full | Run | Run | Run |

These are dependency-based decisions, not promises that an entire feature area
always skips tests. The plan job logs the proof run IDs and fingerprints it uses.

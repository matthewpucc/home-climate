"use strict";

const assert = require("node:assert/strict");
const { mkdtempSync, mkdirSync, writeFileSync, rmSync } = require("node:fs");
const { join } = require("node:path");
const { tmpdir } = require("node:os");
const { test } = require("node:test");
const publish = require("../scripts/publish_release.cjs");

function fixture(t, { existing = null, tag = null, failUpload = false, lookupError = null, draftOnly = false } = {}) {
  const root = mkdtempSync(join(tmpdir(), "house-climate-release-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, "custom_components/house_climate"), { recursive: true });
  mkdirSync(join(root, "dist"));
  writeFileSync(join(root, "custom_components/house_climate/manifest.json"), '{"version":"0.4.2"}');
  writeFileSync(join(root, "CHANGELOG.md"), "# Changelog\n\n## 0.4.2\n\n- Current fix.\n\n## 0.4.1\n\n- Previous fix.\n");
  const assets = ["house-climate-integration.zip", "house-climate-public-repository.zip",
                  "house-climate-local.tar.gz", "house-climate-registry.tar.gz"];
  for (const name of assets) writeFileSync(join(root, "dist", name), "test archive");
  const calls = [];
  const notFound = () => { throw Object.assign(new Error("Not found"), { status: 404 }); };
  const mutate = (name, result = {}) => async (args) => {
    calls.push({ name, args });
    return { data: result };
  };
  const draft = { id: 42, assets: [], draft: true };
  const github = { rest: {
    git: {
      getRef: async () => tag ? { data: { object: { type: "commit", sha: tag } } } : notFound(),
      createRef: mutate("tag"),
    },
    repos: {
      getReleaseByTag: async () => {
        if (lookupError) throw Object.assign(new Error("Lookup failed"), { status: lookupError });
        return existing && !draftOnly ? { data: existing } : notFound();
      },
      listReleases: async () => ({ data: existing ? [{ ...existing, tag_name: "v0.4.2" }] : [] }),
      createRelease: mutate("draft", draft),
      deleteReleaseAsset: mutate("delete"),
      uploadReleaseAsset: async (args) => {
        calls.push({ name: "upload", args });
        if (failUpload) throw new Error("Upload failed");
        return { data: {} };
      },
      updateRelease: mutate("publish", { ...draft, draft: false, html_url: "https://github.com/example/project/releases/tag/v0.4.2" }),
    },
  }};
  github.paginate = async (method, args) => (await method(args)).data;
  const context = {
    repo: { owner: "example", repo: "project" }, sha: "a".repeat(40),
    ref: "refs/heads/main", eventName: "push",
    payload: { repository: { default_branch: "main" } },
  };
  return { root, github, context, core: { info() {} }, calls, assets };
}

test("publishes a checked commit only after uploading every bundle", async (t) => {
  const f = fixture(t);
  await publish(f);
  assert.deepEqual(f.calls.map((c) => c.name), ["tag", "draft", "upload", "upload", "upload", "upload", "publish"]);
  assert.equal(f.calls[0].args.sha, f.context.sha);
  assert.equal(f.calls[0].args.ref, "refs/tags/v0.4.2");
  assert.equal(f.calls[1].args.body, "- Current fix.");
  assert.equal(f.calls[1].args.draft, true);
  assert.deepEqual(f.calls.filter((c) => c.name === "upload").map((c) => c.args.name), f.assets);
  assert.equal(f.calls.at(-1).args.draft, false);
});

test("published releases stay unchanged on later commits with the same version", async (t) => {
  const f = fixture(t, { existing: { id: 42, draft: false, assets: [] }, tag: "b".repeat(40) });
  await publish(f);
  assert.deepEqual(f.calls, []);
});

test("failed uploads leave the release unpublished", async (t) => {
  const f = fixture(t, { failUpload: true });
  await assert.rejects(publish(f), /Upload failed/);
  assert.equal(f.calls.some((c) => c.name === "publish"), false);
});

test("retrying the original run resumes its draft and replaces incomplete assets", async (t) => {
  const f = fixture(t, { existing: { id: 42, draft: true, assets: [{ id: 1, name: "house-climate-integration.zip" }] }, tag: "a".repeat(40) });
  await publish(f);
  assert.equal(f.calls[0].name, "delete");
  assert.equal(f.calls.at(-1).name, "publish");
  assert.equal(f.calls.some((c) => ["tag", "draft"].includes(c.name)), false);
});

test("an existing tag on a different commit is never moved", async (t) => {
  const f = fixture(t, { tag: "b".repeat(40) });
  await assert.rejects(publish(f), /refusing to move/);
  assert.deepEqual(f.calls, []);
});

test("missing bundles or notes stop publishing before mutations", async (t) => {
  for (const missing of ["dist/house-climate-registry.tar.gz", "CHANGELOG.md"]) {
    const f = fixture(t);
    rmSync(join(f.root, missing));
    await assert.rejects(publish(f));
    assert.deepEqual(f.calls, []);
  }
});

test("authorization or API errors are not treated as missing releases", async (t) => {
  const f = fixture(t, { lookupError: 403 });
  await assert.rejects(publish(f), /Lookup failed/);
  assert.deepEqual(f.calls, []);
});

test("pull requests and non-default branches cannot publish", async (t) => {
  for (const change of [{ eventName: "pull_request" }, { ref: "refs/heads/work" }]) {
    const f = fixture(t);
    Object.assign(f.context, change);
    await assert.rejects(publish(f), /checked default-branch/);
    assert.deepEqual(f.calls, []);
  }
});

test("drafts missing from the tag endpoint are found and resumed", async (t) => {
  const f = fixture(t, { existing: { id: 42, draft: true, assets: [] },
                         tag: "a".repeat(40), draftOnly: true });
  await publish(f);
  assert.equal(f.calls.some((c) => c.name === "draft"), false);
  assert.equal(f.calls.at(-1).name, "publish");
});

test("invalid versions and empty changelog entries cannot create a release", async (t) => {
  const badVersion = fixture(t);
  writeFileSync(join(badVersion.root, "custom_components/house_climate/manifest.json"),
                '{"version":"main"}');
  await assert.rejects(publish(badVersion), /major.minor.patch/);
  assert.deepEqual(badVersion.calls, []);
  const missingNotes = fixture(t);
  writeFileSync(join(missingNotes.root, "CHANGELOG.md"), "# Changelog\n\n## 0.4.2\n\n## 0.4.1\n- Older.\n");
  await assert.rejects(publish(missingNotes), /Empty changelog entry/);
  assert.deepEqual(missingNotes.calls, []);
});

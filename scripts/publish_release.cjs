"use strict";

const { readFileSync } = require("node:fs");
const { join } = require("node:path");

const ASSETS = [
  "home-climate-integration.zip",
  "home-climate-public-repository.zip",
  "home-climate-local.tar.gz",
  "home-climate-registry.tar.gz",
];

async function optional(request) {
  try {
    return (await request()).data;
  } catch (error) {
    if (error.status === 404) return null;
    throw error;
  }
}

async function publish({ github, context, core, root = process.cwd() }) {
  const defaultBranch = context.payload.repository.default_branch;
  if (!["push", "workflow_dispatch"].includes(context.eventName) ||
      context.ref !== `refs/heads/${defaultBranch}`) {
    throw new Error("Releases must come from a checked default-branch run");
  }

  const { version } = JSON.parse(readFileSync(
    join(root, "custom_components/house_climate/manifest.json"), "utf8"));
  if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version)) {
    throw new Error("Release version must be a stable major.minor.patch number");
  }
  const lines = readFileSync(join(root, "CHANGELOG.md"), "utf8").split(/\r?\n/);
  const start = lines.indexOf(`## ${version}`);
  if (start < 0) throw new Error(`Missing changelog entry for ${version}`);
  const next = lines.findIndex((line, index) => index > start && line.startsWith("## "));
  const notes = lines.slice(start + 1, next < 0 ? undefined : next).join("\n").trim();
  if (!notes) throw new Error(`Empty changelog entry for ${version}`);

  const repo = context.repo;
  const tag = `v${version}`;
  let release = await optional(() => github.rest.repos.getReleaseByTag({
    ...repo, tag,
  }));
  if (!release) {
    // The tag endpoint may omit drafts; authenticated listing includes them.
    const releases = await github.paginate(github.rest.repos.listReleases, {
      ...repo, per_page: 100,
    });
    release = releases.find((item) => item.tag_name === tag) || null;
  }
  if (release && !release.draft) {
    core.info(`${tag} is already published; leaving its tag and assets unchanged.`);
    return release;
  }

  // Read every required artifact before creating a tag or draft release.
  const assets = ASSETS.map((name) => {
    const data = readFileSync(join(root, "dist", name));
    if (!data.length) throw new Error(`Empty release asset: ${name}`);
    return { name, data };
  });
  const ref = await optional(() => github.rest.git.getRef({
    ...repo, ref: `tags/${tag}`,
  }));
  if (ref && (ref.object.type !== "commit" || ref.object.sha !== context.sha)) {
    throw new Error(`${tag} does not point to this checked commit; refusing to move it`);
  }
  if (!ref) {
    await github.rest.git.createRef({
      ...repo, ref: `refs/tags/${tag}`, sha: context.sha,
    });
  }
  if (!release) {
    release = (await github.rest.repos.createRelease({
      ...repo, tag_name: tag, target_commitish: context.sha,
      name: `Home Climate ${tag}`, body: notes, draft: true, prerelease: false,
    })).data;
  }

  // An interrupted run may leave a draft. Only unpublished assets are replaced.
  for (const { name, data } of assets) {
    const old = release.assets.find((asset) => asset.name === name);
    if (old) await github.rest.repos.deleteReleaseAsset({ ...repo, asset_id: old.id });
    await github.rest.repos.uploadReleaseAsset({
      ...repo, release_id: release.id, name, data,
      headers: { "content-type": name.endsWith(".zip") ? "application/zip" : "application/gzip" },
    });
  }
  release = (await github.rest.repos.updateRelease({
    ...repo, release_id: release.id, name: `Home Climate ${tag}`,
    body: notes, draft: false, prerelease: false, make_latest: "true",
  })).data;
  core.info(`Published ${release.html_url}`);
  return release;
}

module.exports = publish;

import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  hermesSkillsLifecycleService,
  parseSkillFrontmatter,
  reportHermesSkills,
  resolveHermesSkillsDir,
  scanHermesSkills,
} from "../src/hermes-skills.js";
import type { ColonyPluginContext } from "../src/plugin.js";

function makeCtx(overrides?: { hermesSkillsDir?: string }) {
  const client = {
    reportObservations: vi
      .fn()
      .mockResolvedValue({ status: "recorded", domain: "skills", written: 1 }),
  };
  return {
    config: {
      sidecarUrl: "http://stub",
      hermesSkillsDir: overrides?.hermesSkillsDir,
    } as never,
    client: client as never,
    identity: () => ({ host_id: "h" }),
    refreshIdentity: async () => ({}),
    verifyChain: async () => ({}),
    cache: { invalidate: vi.fn(), subscribe: vi.fn() } as never,
    logger: { info: vi.fn(), warn: vi.fn() } as never,
  } as unknown as ColonyPluginContext;
}

const tmpDirs: string[] = [];
async function makeSkillsTree(): Promise<string> {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "hermes-skills-"));
  tmpDirs.push(root);
  return root;
}

afterEach(async () => {
  while (tmpDirs.length > 0) {
    const dir = tmpDirs.pop()!;
    await fs.rm(dir, { recursive: true, force: true });
  }
});

describe("parseSkillFrontmatter", () => {
  it("parses name/description and metadata.hermes.tags block list", () => {
    const fm = parseSkillFrontmatter(
      [
        "---",
        "name: pdf-tools",
        'description: "Work with PDF files"',
        "version: 1.2.0",
        "author: someone",
        "platforms:",
        "  - linux",
        "metadata:",
        "  hermes:",
        "    tags:",
        "      - pdf",
        '      - "documents"',
        "prerequisites:",
        "  commands:",
        "    - pdftotext",
        "---",
        "",
        "# PDF Tools",
      ].join("\n"),
    );
    expect(fm).not.toBeNull();
    expect(fm!.name).toBe("pdf-tools");
    expect(fm!.description).toBe("Work with PDF files");
    expect(fm!.tags).toEqual(["pdf", "documents"]);
  });

  it("parses inline tag lists", () => {
    const fm = parseSkillFrontmatter(
      "---\nname: x\ndescription: y\ntags: [a, 'b', \"c\"]\n---\nbody",
    );
    expect(fm!.tags).toEqual(["a", "b", "c"]);
  });

  it("ignores YAML comment lines (colony provenance block)", () => {
    const fm = parseSkillFrontmatter(
      [
        "---",
        "name: fetch-weather",
        "description: captured",
        "# colony:provenance",
        '#   colony_skill_id: "fetch-weather_a1b2"',
        "---",
        "body",
      ].join("\n"),
    );
    expect(fm!.name).toBe("fetch-weather");
    expect(fm!.tags).toEqual([]);
  });

  it("returns null when there is no frontmatter", () => {
    expect(parseSkillFrontmatter("# Just markdown\n")).toBeNull();
    expect(parseSkillFrontmatter("---\nunclosed: yes\n")).toBeNull();
  });
});

describe("scanHermesSkills", () => {
  it("finds SKILL.md files in <category>/<name>/ trees", async () => {
    const root = await makeSkillsTree();
    const dir = path.join(root, "documents", "pdf-tools");
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(
      path.join(dir, "SKILL.md"),
      "---\nname: pdf-tools\ndescription: Work with PDFs\ntags: [pdf]\n---\nbody",
      "utf8",
    );
    // A skill without an explicit name falls back to its directory name.
    const dir2 = path.join(root, "colony", "fetch-weather");
    await fs.mkdir(dir2, { recursive: true });
    await fs.writeFile(
      path.join(dir2, "SKILL.md"),
      "---\ndescription: captured procedure\n---\nbody",
      "utf8",
    );
    // Noise that must be ignored.
    await fs.writeFile(path.join(root, "README.md"), "not a skill", "utf8");

    const entries = await scanHermesSkills(root);
    expect(entries.map((e) => e.name)).toEqual(["fetch-weather", "pdf-tools"]);
    expect(entries[1]).toMatchObject({
      name: "pdf-tools",
      description: "Work with PDFs",
      tags: ["pdf"],
      path: path.join(dir, "SKILL.md"),
    });
  });

  it("returns [] for a missing directory", async () => {
    const entries = await scanHermesSkills("/nonexistent/hermes/skills");
    expect(entries).toEqual([]);
  });
});

describe("reportHermesSkills", () => {
  it("POSTs the index as a skills-domain observation batch", async () => {
    const root = await makeSkillsTree();
    const dir = path.join(root, "documents", "pdf-tools");
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(
      path.join(dir, "SKILL.md"),
      "---\nname: pdf-tools\ndescription: Work with PDFs\ntags: [pdf, documents]\n---\nbody",
      "utf8",
    );

    const ctx = makeCtx({ hermesSkillsDir: root });
    const written = await reportHermesSkills(ctx);
    expect(written).toBe(1);
    expect(ctx.client.reportObservations).toHaveBeenCalledWith({
      domain: "skills",
      reported_by: "hermes-plugin",
      observations: [
        {
          entity_id: "pdf-tools",
          payload: {
            description: "Work with PDFs",
            tags: ["pdf", "documents"],
            path: path.join(dir, "SKILL.md"),
            source: "hermes",
          },
        },
      ],
    });
  });

  it("does not call Colony when the tree is empty", async () => {
    const root = await makeSkillsTree();
    const ctx = makeCtx({ hermesSkillsDir: root });
    const written = await reportHermesSkills(ctx);
    expect(written).toBe(0);
    expect(ctx.client.reportObservations).not.toHaveBeenCalled();
  });
});

describe("hermesSkillsLifecycleService", () => {
  it("reports at start and re-reports on the 24h interval", async () => {
    vi.useFakeTimers();
    try {
      const root = await makeSkillsTree();
      const dir = path.join(root, "c", "s");
      await fs.mkdir(dir, { recursive: true });
      await fs.writeFile(
        path.join(dir, "SKILL.md"),
        "---\nname: s\ndescription: d\n---\nbody",
        "utf8",
      );
      const ctx = makeCtx({ hermesSkillsDir: root });
      const service = hermesSkillsLifecycleService(ctx);
      expect(service.id).toBe("colony-hermes-skills");

      await service.start();
      // Let the fire-and-forget initial scan settle.
      await vi.waitFor(() => {
        expect(ctx.client.reportObservations).toHaveBeenCalledTimes(1);
      });

      await vi.advanceTimersByTimeAsync(24 * 60 * 60 * 1000);
      await vi.waitFor(() => {
        expect(ctx.client.reportObservations).toHaveBeenCalledTimes(2);
      });

      await service.stop();
      await vi.advanceTimersByTimeAsync(24 * 60 * 60 * 1000);
      expect(ctx.client.reportObservations).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("resolves the default skills dir under the home directory", () => {
    const ctx = makeCtx();
    expect(resolveHermesSkillsDir(ctx)).toBe(
      path.join(os.homedir(), ".hermes", "skills"),
    );
    const ctx2 = makeCtx({ hermesSkillsDir: "/custom/skills" });
    expect(resolveHermesSkillsDir(ctx2)).toBe("/custom/skills");
  });
});

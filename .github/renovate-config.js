module.exports = {
  branchPrefix: "main",
  username: "renovate-asi",
  gitAuthor: "Renovate Bot <bot@renovateapp.com>",
  onboarding: false,
  platform: "github",
  forkProcessing: "disable",
  repositories: ["asi-alliance/OmegaClaw-Core"],
  extends: ["config:recommended"],
  dependencyDashboardAutoclose: true,
  prCreation: "approval",
  prConcurrentLimit: 0,
  reviewers: "anseliv",
  packageRules: [
    {
      description: "lockFileMaintenance",
      matchUpdateTypes: [
        "pin",
        "digest",
        "patch",
        "minor",
        "major",
        "lockFileMaintenance",
      ],
      minimumReleaseAge: 3,
    },
  ],
};

// Prüft die Node-Version vor dem Start (Vite 7 / Vitest 5 brauchen Node >= 22.12).
const MIN = [22, 12];
const [major, minor] = process.versions.node.split(".").map(Number);
if (major < MIN[0] || (major === MIN[0] && minor < MIN[1])) {
  console.error(
    `Node ${process.versions.node} ist zu alt – benötigt wird >= ${MIN.join(".")}.\n` +
      "Linux: nvm install --lts   |   Windows: LTS-Installer von https://nodejs.org\n" +
      "Details: README, Abschnitt Voraussetzungen.",
  );
  process.exit(1);
}

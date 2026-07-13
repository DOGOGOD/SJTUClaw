import SyntaxHighlighter from "react-syntax-highlighter/dist/esm/prism-light";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import c from "react-syntax-highlighter/dist/esm/languages/prism/c";
import cpp from "react-syntax-highlighter/dist/esm/languages/prism/cpp";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import go from "react-syntax-highlighter/dist/esm/languages/prism/go";
import java from "react-syntax-highlighter/dist/esm/languages/prism/java";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import jsx from "react-syntax-highlighter/dist/esm/languages/prism/jsx";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import markup from "react-syntax-highlighter/dist/esm/languages/prism/markup";
import powershell from "react-syntax-highlighter/dist/esm/languages/prism/powershell";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import rust from "react-syntax-highlighter/dist/esm/languages/prism/rust";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";

const languages = {
  bash,
  c,
  cpp,
  css,
  go,
  java,
  javascript,
  jsx,
  json,
  markdown,
  markup,
  powershell,
  python,
  rust,
  sql,
  tsx,
  typescript,
  yaml,
};

for (const [name, grammar] of Object.entries(languages)) {
  SyntaxHighlighter.registerLanguage(name, grammar);
}

for (const [alias, name] of Object.entries({
  html: "markup",
  js: "javascript",
  md: "markdown",
  py: "python",
  sh: "bash",
  shell: "bash",
  ts: "typescript",
  xml: "markup",
  yml: "yaml",
})) {
  SyntaxHighlighter.registerLanguage(alias, languages[name as keyof typeof languages]);
}

export default SyntaxHighlighter;

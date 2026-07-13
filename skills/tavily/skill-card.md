## Description: <br>
AI-optimized web search using the Tavily Search API for research, current-events lookup, domain-specific search, image search, raw content extraction, and AI-generated answer summaries. <br>

This skill is ready for commercial/non-commercial use. <br>

## Publisher: <br>
[bert-builder](https://clawhub.ai/user/bert-builder) <br>

### License/Terms of Use: <br>


## Use Case: <br>
Developers, researchers, and agents use this skill to run Tavily-powered web, news, domain-filtered, image, and raw-content searches for research, fact-checking, current-events lookup, and source gathering. <br>

### Deployment Geography for Use: <br>
Global <br>

## Known Risks and Mitigations: <br>
Risk: Search queries and selected options are sent to Tavily under the configured API key. <br>
Mitigation: Do not submit secrets, private internal documents, regulated data, or sensitive personal information in search queries or options. <br>
Risk: Search results, source URLs, image URLs, and raw content can contain inaccurate or untrusted third-party material. <br>
Mitigation: Review sources before relying on results, and treat URLs or content fetched from results as untrusted. <br>
Risk: The skill depends on the tavily-python package at runtime. <br>
Mitigation: Pin or review the dependency in stricter environments before deployment. <br>


## Reference(s): <br>
- [Tavily API Reference](references/api-reference.md) <br>
- [Tavily](https://tavily.com) <br>
- [Tavily Documentation](https://docs.tavily.com) <br>
- [Tavily Python SDK](https://github.com/tavily-ai/tavily-python) <br>
- [ClawHub Skill Page](https://clawhub.ai/bert-builder/skills/tavily) <br>


## Skill Output: <br>
**Output Type(s):** [text, json, shell commands, configuration, guidance] <br>
**Output Format:** [Markdown guidance and JSON search responses] <br>
**Output Parameters:** [1D] <br>
**Other Properties Related to Output:** [Search responses may include AI answers, result snippets, source URLs, image URLs, raw content, response time, and usage credits.] <br>

## Skill Version(s): <br>
1.0.0 (source: server release evidence) <br>

## Ethical Considerations: <br>
Users should evaluate whether this skill is appropriate for their environment, review any generated or modified files before relying on them, and apply their organization's safety, security, and compliance requirements before deployment. <br>

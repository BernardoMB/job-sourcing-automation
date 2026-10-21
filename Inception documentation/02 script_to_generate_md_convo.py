from markdownify import markdownify

with open("Agentic_Job_Sourcer.html", encoding="utf-8") as f:
    html = f.read()

with open("agentic_job_sourcer_conversation.md", "w", encoding="utf-8") as f:
    f.write(markdownify(html))
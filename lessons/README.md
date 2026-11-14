# Lessons

Real lessons written by the Ask and Teach Chat from human corrections, one file per topic (`<topic>.md`).

Each lesson in a file starts with its record: who taught it, when, how sure the lesson writer was, its version, its status (only approved lessons are used in answers), and the judge's score for the same question **before and after** the lesson (from "Test before and after" on the Lessons screen).

## How a lesson gets here

1. In the chat, correct a wrong answer ("Actually, refunds take 5 business days"), or start a "Teach me" chat.
2. The chat writes a lesson draft (Claude Opus 5.5, `chat.write_lesson`): a rule, an example, a confidence.
3. On the Lessons screen, approve it, then click **Test before and after**.
4. Click **Download the lesson files (.zip)** and unzip it here, at the repo root: the files land in this folder.

The brief asks for at least 10 real lessons with before-and-after scores. None is in this folder yet: they need real corrections and a Claude key.

# Safe date correction

Explicit dates are checked before the form and invitation catalog are accessed.
Past dates and dates more than 24 calendar months ahead are rejected. The upper
boundary matches the calendar navigation supported by Yandex Forms.

A recognizable but invalid date keeps the candidate at the date question rather
than handing them over to an operator. The year is never silently replaced.

At startup, confirmed pre-submission calendar failures are returned to date
collection with identity, phone and warehouse retained. This migration sends no
bulk messages to historical candidates. A new date resumes submission without
asking for identity fields again. Submitted, uncertain, completed and manually
taken-over records are excluded; unknown submission results are never replayed.

# Accessibility

## Accessibility Commitment

MarkItDownTool is intended to be usable by as many people as possible, regardless of their abilities, devices, or technical experience.

Accessibility matters because document conversion, OCR processing, and knowledge management tools should be available to everyone, including people who use assistive technologies or alternative input methods.

As an independently maintained open-source project, MarkItDownTool strives to continuously improve accessibility while balancing available development resources.

This document describes:

- Accessibility priorities
- Contributor expectations
- How to report accessibility issues
- Known limitations
- Ongoing improvement efforts

---

## Priorities

MarkItDownTool prioritizes accessibility in the following areas:

### Keyboard Accessibility

Users should be able to complete common tasks using a keyboard whenever reasonably possible.

Examples include:

- Selecting files
- Navigating dialogs
- Starting conversions
- Accessing application controls

### Clear Content and Language

The project aims to provide:

- Clear labels
- Understandable instructions
- Meaningful error messages
- Consistent terminology

### Visual Accessibility

The project strives to:

- Maintain readable interface text
- Provide sufficient visual contrast where practical
- Avoid relying solely on color to communicate important information

### Compatibility

The project aims to remain compatible with:

- Standard Windows accessibility features
- Screen magnification tools
- Common operating system accessibility settings

Accessibility improvements are treated as an ongoing goal rather than a statement of formal compliance or certification.

---

## Contributor Expectations

Contributors are encouraged to consider accessibility when submitting changes.

When changing user-facing functionality, contributors should:

- Use clear and descriptive labels.
- Avoid unnecessary complexity.
- Ensure important actions can be identified without relying only on color.
- Write error messages that explain what happened and what users can do next.
- Test the feature using common input methods whenever possible.

When submitting Pull Requests that affect the user interface, contributors are encouraged to include:

- Screenshots
- Description of user-visible changes
- Relevant usability considerations

Accessibility improvements are welcome and encouraged.

---

## Reporting Accessibility Issues

If you encounter an accessibility barrier while using MarkItDownTool, please report it.

You do not need to disclose any personal health information or disability information.

Please include:

- Description of the task you were trying to complete
- What happened
- What you expected to happen
- Operating system
- MarkItDownTool version
- Keyboard, screen reader, magnifier, or other assistive tools used (if applicable)

Issues may be submitted through:

### GitHub Issues

Project Repository:

https://github.com/hungnp1999/MarkitdownTool

### Direct Contact

Email:

nguyenphuhung1999@gmail.com

Screenshots and recordings are optional but may help reproduce the issue.

---

### Severity

Accessibility issues may be prioritized based on their impact.

#### Critical

The issue prevents a user from completing a core task.

Examples:

- Unable to start a conversion
- Unable to select files
- Essential controls inaccessible

#### High

A significant barrier exists, but a workaround is available.

Examples:

- Important controls difficult to identify
- Dialogs difficult to navigate

#### Medium

The task remains possible but produces meaningful inconvenience.

Examples:

- Confusing instructions
- Poor keyboard navigation

#### Low

Minor usability issues with limited impact.

Examples:

- Cosmetic inconsistencies
- Non-critical wording improvements

Severity may be assigned or adjusted by the maintainer during review.

---

### How We Respond

Accessibility reports will be reviewed as time and resources allow.

The project aims to:

- Acknowledge reports within a reasonable timeframe
- Request additional information if necessary
- Investigate reproducible issues
- Identify practical workarounds when available
- Include fixes in future releases when feasible

Since MarkItDownTool is currently maintained by a single independent developer, response times may vary.

---

## Ownership and Maintenance

Accessibility is currently the responsibility of the project maintainer.

### Maintainer

Nguyễn Phú Hùng

GitHub:

https://github.com/hungnp1999/MarkitdownTool

Email:

nguyenphuhung1999@gmail.com

Accessibility considerations are reviewed during normal project maintenance and feature development.

If project ownership changes in the future, accessibility responsibilities will be transferred to the new maintainer.

---

## Supported Environments

The following environments are the primary focus of development and testing.

### Operating Systems

- Windows 10
- Windows 11

### Input Methods

- Mouse
- Keyboard

### File Processing

- PDF
- DOCX
- XLSX
- PPTX

### OCR

- Tesseract OCR
- OCRmyPDF

Other environments may work but may not receive the same level of testing.

---

## Known Limitations

The project is actively evolving and may contain accessibility limitations.

Known areas for improvement may include:

- Limited screen reader testing
- Dialog-based workflows that depend on operating system behavior
- OCR result quality depending on input document quality
- Third-party library interfaces outside the direct control of the project

Users who encounter barriers are encouraged to report them.

---

## Feedback and Improvements

Suggestions for improving accessibility are always welcome.

You can:

- Open a GitHub Discussion
- Submit a Feature Request
- Open an Accessibility Issue
- Contact the maintainer directly

Accessibility feedback helps improve the project for all users.

If you encounter an active accessibility barrier, please use the reporting process described above rather than general feature discussions.

---

## Continuous Improvement

Accessibility is not a one-time task.

MarkItDownTool is committed to improving accessibility over time through:

- Community feedback
- User testing
- Contributor recommendations
- Ongoing project maintenance

Every accessibility improvement helps make the project more useful for everyone.

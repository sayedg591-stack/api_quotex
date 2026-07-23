---
name: CSS [hidden] attribute override bug
description: Why the native `hidden` attribute can stop working and how to guard against it
---

The browser's UA stylesheet sets `[hidden] { display: none }` with attribute-selector
specificity (0,1,0). Any author rule with equal specificity that sets `display` on the
same element (e.g. a class like `.view { display: flex; }` or `.drawer { display: flex; }`)
ties in specificity, and since the author stylesheet loads after the UA stylesheet, the
author's `display` wins — silently un-hiding elements that JS toggled via `el.hidden = true`
or `hidden` attribute in markup.

**Why:** Discovered while building a tabbed/paginated UI where every view/panel container
declared `display: flex` for its own layout. Toggling `.hidden` via the DOM `hidden` property
had no visible effect — the panel stayed visible — even though the JS logic and element
selection were correct.

**How to apply:** Any time you rely on the native `hidden` attribute/property to show/hide
elements that also carry their own `display` declaration in CSS, add a global override near
the top of the stylesheet: `[hidden] { display: none !important; }`. Do this proactively in
any tabbed/paginated/drawer UI, don't wait to discover it by trial and error.

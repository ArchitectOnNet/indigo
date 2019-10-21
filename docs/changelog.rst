Changelog
=========

5.0.0 (21 October 2019)
-----------------------

* FEATURE: count of comments on a document, and comment navigation
* FEATURE: resolver for looking up documents in the local database
* FEATURE: include images in PDFs and ePUBs
* FEATURE: Support for arbitrary expression dates
* Custom work properties for a place moved into settings

4.1.0 (3 October 2019)
----------------------

* FEATURE: Paste tables directly from Word when in edit mode.
* FEATURE: Scaffolding for showing document issues.
* FEATURE: Show document hierarchy in editor.
* FEATURE: Support customisable importing of HTML files.
* FEATURE: Customisable PDF footers
* Clearer indication of repealed works.
* indigo-web 3.6.1 - explicit styling for crossHeading elements
* Badge icons are now stylable images
* Javascript traditions inherit from the defaults better, and are simpler to manage.

4.0.0 (12 September 2019)
-------------------------

This release drops support for Python 2.x. Please upgrade to at least Python 3.6.

* BREAKING: Drop support for Python 2.x
* FEATURE: Calculate activity metrics for places
* FEATURE: Importing bulk works from Google Sheets now allows you to choose a tab to import from
* Preview when importing bulk works
* Requests are atomic and run in transactions by default
* Improved place listing view, including activity for the place
* Localities page for a place

3.0 (5 July 2019)
-----------------

This is the first major release of Indigo with over a year of active development. Upgrade to this version by installing updated dependencies and running migrations.

* FEATURE: Support images in documents
* FEATURE: Download as XML
* FEATURE: Annotations/comments on documents
* FEATURE: Download documents as ZIP archives
* FEATURE: You can now highlight lines of text in the editor and transform them into a table, using the Edit > Insert Table menu item.
* FEATURE: Edit menu with Find, Replace, Insert Table, Insert Image, etc.
* FEATURE: Presence indicators for other users editing the same document.
* FEATURE: Assignable tasks and workflows.
* FEATURE: Social/oauth login supported.
* FEATURE: Localisation support for different languages and legal traditions.
* FEATURE: Badge-based permissions system
* FEATURE: Email notifications
* FEATURE: Improved diffs in document and work version histories
* FEATURE: Batch creation of works from Google Sheets
* FEATURE: Permissions-based API access
* FEATURE: Attach publication documents to works
* FEATURE: Measure work completeness
* BREAKING: Templates for localised rendering have moved to ``templates/indigo_api/akn/``
* BREAKING: The LIME editor has been removed.
* BREAKING: Content API for published documents is now a separate module and versioned under ``/v2/``
* BREAKING: Some models have moved from ``indigo_app`` to ``indigo_api``, you may need to updated your references appropriately.

2.0 (6 April 2017)
------------------

* Upgraded to Django 1.10
* Upgraded a number of dependencies to support Django 1.10
* FEATURE: significantly improved mechanism for maintaining amended versions of documents
* FEATURE: you can now edit tables directly inline in a document
* FEATURE: quickly edit a document section without having to open it via the TOC
* FEATURE: support for newlines in tables
* FEATURE: improved document page layout
* FEATURE: pre-loaded set of publication names per country
* Assent and commencement notices are no longer H3 elements, so PDFs don't include them in their TOCs. #28
* FIX: bug when saving an edited section
* FIX: ensure TOC urls use expression dates
* FIX: faster document saving

After upgrading to this version, you **must** run migrations::

    python manage.py migrate

We also recommend updating the list of countries::

    python manage.py update_countries_plus

1.1 (2016-12-19)
----------------

* First tagged release

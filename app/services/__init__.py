"""Business logic shared by the web routes and the CLI commands.

* :mod:`app.services.validation` -- read, map, and validate a source table
* :mod:`app.services.ingest`     -- stage files and write approved data
* :mod:`app.services.campd`      -- retrieve data from the EPA CAMPD API
* :mod:`app.services.search`     -- the data-explorer query builder
* :mod:`app.services.exports`    -- CSV / Excel download responses
"""

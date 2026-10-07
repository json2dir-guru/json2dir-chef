# json2dir-chef

json2dir in Chef: the recipe `json2dir.chef` ("JSON-to-Directory Souffle", generated from the stack-machine source `json2dir.stk` by `gen/chef.py`) validates the JSON document and serves a shell script, which the POSIX sh launcher `json2dir` runs. The launcher feeds the input as one decimal byte per line because Chef only reads numbers.

    python3 gen/chef.py json2dir.stk > json2dir.chef
    JSON2DIR_CHEF=/path/to/acme-chef/bin/chef ./json2dir < tree.json

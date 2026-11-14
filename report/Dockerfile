# LaTeX toolchain for the Assignment 3 report (no local TeX installation needed).
#   docker build -t petstore-a3-latex report
#   docker run --rm -v "${PWD}/report:/doc" petstore-a3-latex
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      latexmk texlive-latex-base texlive-latex-recommended texlive-latex-extra \
      texlive-pictures texlive-fonts-recommended lmodern \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /doc
CMD ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "assignment3-report.tex"]

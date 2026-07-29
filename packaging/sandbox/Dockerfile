FROM python:3.12-bookworm

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    MPLBACKEND=Agg

LABEL org.opencontainers.image.title="SJTUClaw Sandbox Python" \
      org.opencontainers.image.description="Common Python dependencies for SJTUClaw microsandbox sessions"

COPY requirements-common.txt /opt/sjtuclaw/requirements-common.txt

RUN python -m pip install --no-cache-dir \
        -r /opt/sjtuclaw/requirements-common.txt \
    && python -m pip check \
    && python -W "error:urllib3" -c "import bs4, docx, httpx, lxml, matplotlib, numpy, openpyxl, pandas, PIL, pypdf, reportlab, requests, scipy, seaborn, sklearn, yaml"

WORKDIR /workspace

"""Minimal setup for the shared common package.

Used with `pip install -e .` for local development (editable install)
and `pip install .` for Docker builds (non-editable).
"""

from setuptools import find_packages, setup

setup(
    name="statement-processor-common",
    version="0.1.0",
    packages=find_packages(),
    python_requires=">=3.13",
    install_requires=[
        "pydantic",
    ],
)

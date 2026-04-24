from setuptools import setup, find_packages

setup(
    name="ai-quant-trading",
    version="1.0.0",
    description="AI Quantitative Trading System for US Stocks via LongPort",
    author="Gavin",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "longport>=1.0.0",
        "backtrader>=1.9.78",
        "pandas>=1.5.0",
        "numpy>=1.23.0",
        "pandas-ta>=0.3.14",
        "APScheduler>=3.10.0",
        "loguru>=0.7.0",
        "pydantic>=2.0.0",
        "pyyaml>=6.0",
        "python-dotenv>=1.0.0",
        "matplotlib>=3.7.0",
        "mplfinance>=0.12.9",
        "pytz>=2023.3",
        "requests>=2.31.0",
    ],
)

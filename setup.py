from setuptools import setup


with open("README.pypi.md", "r", encoding='UTF-8') as f:
    readme = f.read()

classifiers = [
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.7",
    "Programming Language :: Python :: 3.8",
    "Programming Language :: Python :: 3.9",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: Implementation :: PyPy",
    "Intended Audience :: Developers",
    "Topic :: Software Development :: Libraries",
]

keywords = ("asterisk", "manager", "interface",
            "api", "asterisk-manager-interface",
            "ami", "asterisk-ami", "ami-client",
            "asyncio", "async", "http")

setup(
    name='ami-client',
    version='0.0.1rc1',
    packages=['ami', 'ami.client'],
    url='https://github.com/XpycTee/ami-client',
    license='Apache-2.0 license',
    author='XpycTee',
    author_email='i@xpyctee.ru',
    description='Asynchronous client for working with Asterisk Manager Interface',
    long_description=readme,
    long_description_content_type="text/markdown",
    classifiers=classifiers,
    keywords=' '.join(keywords),
    install_requires=['aiohttp'],
    python_requires='>=3.7'
)

# Contributing to CLRI

Thank you for your interest in contributing to the Crypto Leverage Risk Index project.

## Getting Started

1. Fork the repository
2. Clone your fork locally
3. Create a feature branch from `main`
4. Make your changes
5. Submit a pull request

## Development Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/clri.git
cd clri

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp .env.example .env
# Edit .env with your test credentials

# Start PostgreSQL
docker run -d -p 5432:5432 \
  -e POSTGRES_DB=clri \
  -e POSTGRES_PASSWORD=dev \
  postgres:15

# Run the worker
python -m worker.main
```

## Code Style

- Follow PEP 8 guidelines
- Use type hints for function signatures
- Keep functions focused and reasonably sized
- Write clear commit messages using conventional commits:
  - `feat:` for new features
  - `fix:` for bug fixes
  - `docs:` for documentation
  - `refactor:` for code refactoring
  - `test:` for adding tests

## Pull Request Process

1. Ensure your code follows the project style
2. Update documentation if needed
3. Test your changes locally
4. Write a clear PR description explaining the changes
5. Reference any related issues

## Reporting Issues

When reporting issues, please include:

- Clear description of the problem
- Steps to reproduce
- Expected vs actual behavior
- Relevant logs or error messages
- Environment details (OS, Python version, etc.)

## Feature Requests

Feature requests are welcome. Please:

- Check existing issues first to avoid duplicates
- Clearly describe the use case
- Explain why this would benefit the project

## Questions

For questions about the codebase or implementation details, open a discussion or issue.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

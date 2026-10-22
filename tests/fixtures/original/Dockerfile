# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set the working directory in the container
# WORKDIR /usr/src/app

# Updating the system and installing basic tools
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y curl git vim \
    && rm -rf /var/lib/apt/lists/*

# Install Ansible and any other dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ansible \
    ssh \
    openssh-client \
    rsync \
    && rm -rf /var/lib/apt/lists/*

# Install poetry for dependency management
# Install development tools
RUN pip install flake8 black isort mypy poetry ansible

# Install dependencies in a virtual environment to be copied later
# RUN python -m venv /venv
# ENV PATH="/venv/bin:$PATH"

# Set the timezone (replace with your timezone)
ENV TZ=Europe/Warsaw
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Create a non-root user and switch to it
RUN useradd -m developer
USER developer

# Install Python dependencies
# This is done before copying the project files to cache the dependencies
# and only re-build them when they change
# COPY requirements.txt ./
# or
# COPY --chown=developer:developer requirements.txt ./
# RUN pip install --no-cache-dir -r requirements.txt

# Copy the current directory contents into the container at /usr/src/app
# COPY . .
# or
# COPY --chown=developer:developer . .

# Health check for the application
# HEALTHCHECK --interval=30s --timeout=3s \
#   CMD curl -f http://localhost:8000/ || exit 1

# Expose port 8000 for web applications
# EXPOSE 8000

# Run the application
# CMD ["python", "./your-app.py"]

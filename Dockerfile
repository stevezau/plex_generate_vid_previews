FROM linuxserver/ffmpeg:6.1.1

# Install Python and pip
RUN apt-get update && apt-get install -y mediainfo software-properties-common

# We need python >=3.12.1 due to bug here https://github.com/python/cpython/issues/105829
RUN add-apt-repository --yes ppa:deadsnakes/ppa && apt-get update && apt install -y python3.12
RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python3.12

# Set the working directory in the container
WORKDIR /app

# Copy the requirements.txt file to the working directory
COPY requirements.txt .

# Install the Python dependencies
RUN pip3.12 install -r requirements.txt

# Copy the Python script and .env file to the working directory
COPY plex_generate_previews.py .

# Run the Python script when the container starts
ENTRYPOINT ["/bin/bash", "-c", "/usr/bin/python3.12 /app/plex_generate_previews.py"]

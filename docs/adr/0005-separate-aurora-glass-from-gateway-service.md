# Separate Aurora Glass from Gateway Service

Aurora Glass is delivered as a separate frontend container, while the Gateway remains an API service. Both artifacts are built from this repository, published from one CI workflow with shared version tags, and intended to share one public origin so Aurora Glass can keep using relative API paths without introducing CORS as part of this split.

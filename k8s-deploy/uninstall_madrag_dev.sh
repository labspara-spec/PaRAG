#!/bin/bash

NAMESPACE=rag
helm uninstall madrag-dev --namespace $NAMESPACE

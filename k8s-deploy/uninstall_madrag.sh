#!/bin/bash

NAMESPACE=rag
helm uninstall madrag --namespace $NAMESPACE

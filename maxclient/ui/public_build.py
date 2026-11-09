"""Portable alpha excludes WebEngine until its redistribution pack is reviewed."""
import sys


def web_features_available():
    return not getattr(sys, 'frozen', False)

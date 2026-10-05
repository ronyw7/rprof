#!/bin/bash
if grep -q done /app/out.txt; then echo 1; else echo 0; fi > /logs/verifier/reward.txt

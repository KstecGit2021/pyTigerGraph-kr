# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9] - 2022-05-16
We are excited to announce the pyTigerGraph v0.9 release! This release adds many new features for graph machine learning and graph data science, a refactoring of core code, and more robust testing. Additionally, we have officially “graduated” it to an official TigerGraph product. This means brand-new documentation, a new GitHub repository, and future feature enhancements. While becoming an official product, we are committed to keeping pyTigerGraph true to its roots as an open-source project. Check out the contributing page and GitHub issues if you want to help with pyTigerGraph’s development. 
## Changed
* Feature: Include Graph Data Science Capability
    - Many new capabilities added for graph data science and graph machine learning. Highlights include data loaders for training Graph Neural Networks in DGL and PyTorch Geometric, a "featurizer" to generate graph-based features for machine learning, and utilities to support those activities.

* Documentation: We have moved the documentation to [the official TigerGraph Documentation site](https://docs.tigergraph.com/pytigergraph/current/intro/) and updated many of the contents.

* Testing: There is now well-defined testing for every function in the package. A more defined testing framework is coming soon.

* Code Structure: A major refactor of the codebase was performed. No breaking changes were made to accompplish this.

## [0.0.9.7.8] - 2021-09-27
## Changed
* Fix :  added safeChar method to fix URL encoding

## [0.0.9.7.7] - 2021-09-20
## Changed
* Fix :  removed the localhost to 127.0.0.1 translation


## [0.0.9.7.6] - 2021-09-01
## Changed
* Fix :  SSL issue with Rest++ for self-signed certs 
* Fix :  Updates for pyTigerDriver bounding 
* Feature : added the checks to debug
* Fix :  added USE GRAPH cookie

## [0.0.9.7.0] - 2021-07-07
### Changed
* runInstalledQuery(usePost=True) will post params as body 


## [0.0.9.6.9] - 2021-06-03
### Changed
* Made SSL Port configurable to grab SSL cert from different port in case of firewall on 443


## [0.0.9.6.3] - 2020-12-14
### Fix : 
* Fix :  (more) runInstalledQuery() params 

## [0.0.9.6.2] - 2020-10-08
### Fix : 
* Fix :  (more) runInstalledQuery() params processing bugs


## [0.0.9.6] - 2020-10-08
### Fix : 
* Fix :  (more) runInstalledQuery() params processing bugs


## [0.0.9.5] - 2020-10-07
### Fix : 
* Fix :  runInstalledQuery() params processing


## [0.0.9.4] - 2020-10-03
### Changed
* Add Path finding endpoint
* Add Full schema retrieval

### Fix : 
* Fix GSQL client
* Fix parseQueryOutput
* Code cleanup

## [0.0.9.3] - 2020-09-30
### Changed
* Remove urllib as dependency
### Fix : 

## [0.0.9.2] - 2020-09-30
### Changed
* Fix space in query param issue #22
### Fix : 

## [0.0.9.1] - 2020-09-03
### Changed
* SSL Cert support on REST requests
### Fix : 

## [0.0.9.0] - 2020-08-22
### Changed
### Fix : 
* Fix getVertexDataframeById()
* Fix GSQL versioning issue

## [0.0.8.4] - 2020-08-19
### Changed
### Fix : 
* Fix GSQL Bug

## [0.0.8.4] - 2020-08-19
### Changed
### Fix : 
* Fix GSQL getVer() bug

## [0.0.8.3] - 2020-08-08
### Changed
### Fix : 
* Fix initialization of gsql bug

## [0.0.8.2] - 2020-08-08
### Changed
### Fix : 
* Fix initialization of gsql bug

## [0.0.8.1] - 2020-08-08
### Changed
### Fix : 
* Fix bug in gsqlInit()

## [0.0.8.0] - 2020-08-07
### Changed
* Add getVertexSet()
### Fix : 

## [0.0.7.0] - 2020-07-26
### Changed
* Move GSQL functionality to main package
### Fix : 

## [0.0.6.9] - 2020-07-23
### Changed
* Main functionality exists and is in relatively stable
### Fix : 
* Minor bug fixes


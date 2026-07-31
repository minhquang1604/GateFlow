###########################################################################
# Network module — VPC, subnets, IGW, route tables, DB subnet group.
#
# Single-purpose: give downstream modules (security_groups, rds, compute)
# everything they need to attach to the right subnet, SG, and route.
###########################################################################

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # Default CIDR derivation: take /16 vpc_cidr, split into /24s.
  # Public uses offsets 1,2; private uses 10,11 to leave room for
  # additional subnets in between.
  derived_public_cidrs  = [cidrsubnet(var.vpc_cidr, 8, 1), cidrsubnet(var.vpc_cidr, 8, 2)]
  derived_private_cidrs = [cidrsubnet(var.vpc_cidr, 8, 10), cidrsubnet(var.vpc_cidr, 8, 11)]

  public_cidrs  = length(var.public_subnet_cidrs) > 0 ? var.public_subnet_cidrs : local.derived_public_cidrs
  private_cidrs = length(var.private_subnet_cidrs) > 0 ? var.private_subnet_cidrs : local.derived_private_cidrs

  public_subnet_names  = [for i in range(var.az_count) : "${var.name_prefix}-public-${i + 1}"]
  private_subnet_names = [for i in range(var.az_count) : "${var.name_prefix}-private-${i + 1}"]
}

# ---------------------------------------------------------------------- #
# VPC                                                                    #
# ---------------------------------------------------------------------- #
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = var.enable_dns_support
  enable_dns_hostnames = var.enable_dns_hostnames

  tags = {
    Name = "${var.name_prefix}-vpc"
  }
}

# ---------------------------------------------------------------------- #
# Subnets                                                                #
# ---------------------------------------------------------------------- #
resource "aws_subnet" "public" {
  count = var.az_count

  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.public_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = var.public_subnet_map_public_ip_on_launch

  tags = {
    Name = local.public_subnet_names[count.index]
    Tier = var.public_subnet_tag_tier
  }
}

resource "aws_subnet" "private" {
  count = var.az_count

  vpc_id            = aws_vpc.main.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = {
    Name = local.private_subnet_names[count.index]
    Tier = var.private_subnet_tag_tier
  }
}

# ---------------------------------------------------------------------- #
# Internet gateway + route tables                                         #
# ---------------------------------------------------------------------- #
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.name_prefix}-igw"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = var.public_route_default_cidr
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${var.name_prefix}-rt-public"
  }
}

resource "aws_route_table_association" "public" {
  count = var.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Private route table intentionally has no default route — the Free-Tier
# stack has no NAT Gateway. Add a route + NAT later if outbound internet
# access from private subnets is ever needed.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.name_prefix}-rt-private"
  }
}

resource "aws_route_table_association" "private" {
  count = var.az_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ---------------------------------------------------------------------- #
# DB subnet group — consumed by RDS module.                              #
# ---------------------------------------------------------------------- #
resource "aws_db_subnet_group" "main" {
  name        = "${var.name_prefix}-db-subnets"
  description = "Private subnets hosting the MLOps framework RDS instance."
  subnet_ids  = aws_subnet.private[*].id

  tags = {
    Name = "${var.name_prefix}-db-subnets"
  }
}
